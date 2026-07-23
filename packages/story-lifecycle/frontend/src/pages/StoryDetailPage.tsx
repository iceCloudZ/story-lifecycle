import { useState, useEffect, useRef } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useParams, useNavigate, useSearchParams } from 'react-router-dom'
import { storyApi, apiAction, planApi, diffApi } from '../api/client'
import type { AgentAction, ActionButton } from '../api/client'
import StorySidebar from '../components/StorySidebar'
import OverviewTab from '../components/OverviewTab'
import CodeChangesTab from '../components/CodeChangesTab'
import DocsTab from '../components/DocsTab'
import ClarifyDialog from '../components/ClarifyDialog'
import './StoryDetailPage.css'

// Visible modules for the semi-automatic workflow. Terminal 放回 sidebar:
// 终端已并入概览底部(上下分区),不再单独成 tab。
const MODULES = [
  { id: 'overview', icon: '📊', label: '概览' },
  { id: 'code', icon: '📦', label: '代码变更' },
  { id: 'docs', icon: '📄', label: '文档' },
]

// 概览操作按钮:只放「推进执行类」操作(继续/重试/紧急停止)。
// 「删除」在列表卡片上已有,概览不重复。blocked/aborted 是 CQRS 重构前的旧值,
// 已合并到 paused/failed,这里保留兼容老数据但语义等价。
const ACTIONS: Record<string, ActionButton[]> = {
  planning: [],
  active: [
    { label: '紧急停止', method: 'POST', path: '/emergency-stop', confirm: '立即杀掉运行中的 claude 进程并暂停（可恢复）。确定？', variant: 'danger' },
  ],
  paused: [
    { label: '继续执行', method: 'PUT', path: '/advance', variant: 'primary' },
  ],
  // blocked 旧值 → 已合并 paused(重试语义同 advance)
  blocked: [
    { label: '重试', method: 'PUT', path: '/advance', variant: 'primary' },
  ],
  failed: [
    { label: '重试', method: 'PUT', path: '/advance', variant: 'primary' },
  ],
  completed: [],
  // aborted 旧值 → 已合并 failed
  aborted: [
    { label: '重试', method: 'PUT', path: '/advance', variant: 'primary' },
  ],
}

export default function StoryDetailPage() {
  const { key } = useParams<{ key: string }>()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const storyKey = key ?? ''
  const [searchParams, setSearchParams] = useSearchParams()

  const activeTab = searchParams.get('tab') || 'overview'
  const setActiveTab = (tab: string) => setSearchParams({ tab })
  // 交付物精准跳转:跳 tab + 可选打开特定 doc(如 spec)。
  // setSearchParams 替换整个 query string,所以 tab + doc 一次写全。
  const handleNavigate = (tab: string, doc?: string) => {
    setSearchParams(doc ? { tab, doc } : { tab })
  }
  // 旧链接可能带已删除的 tab(test/llm-audit/bugs),回落到概览,避免空白内容区。
  const validTab = MODULES.some((m) => m.id === activeTab) ? activeTab : 'overview'

  const { data: detail, refetch } = useQuery({
    queryKey: ['story', storyKey],
    queryFn: () => storyApi.get(storyKey),
    refetchInterval: 5000,
  })

  // 进详情触发关联 bug 同步（节流 5min，避免每次都打 TAPD）
  useQuery({
    queryKey: ['sync-related-bugs', storyKey],
    queryFn: async () => {
      const r = await fetch(`/api/story/${storyKey}/sync-related-bugs`, { method: 'POST' })
      return r.ok ? r.json() : null
    },
    staleTime: 5 * 60 * 1000,
    retry: false,
  })

  // 进详情页就异步预取所有 project 的 diff(代码变更 tab 用)。
  // 每仓 fetch ~2s 是瓶颈,三个仓独立;进页并行 prefetch → 切到 code tab / 切 project
  // 时直接命中缓存(staleTime 见 CodeChangesTab 的 diff 查询),不再串行等 fetch。
  // 用独立的 context 查询拿 story_projects,避免和 CodeChangesTab 的 context 查询重复
  // (同 queryKey ['context', storyKey] 会自动复用缓存)。
  const { data: storyCtx } = useQuery({
    queryKey: ['context', storyKey],
    queryFn: () => storyApi.context(storyKey),
    enabled: !!storyKey,
    staleTime: 60 * 1000,
  })
  useEffect(() => {
    const bindings = storyCtx?.story_projects ?? []
    if (bindings.length === 0) return
    // 并行 prefetch 每个 project 的 diff(React Query 的 prefetchQuery 不会阻塞渲染,
    // 失败静默 — 命中后 CodeChangesTab 的 useQuery 直接走缓存)。
    for (const b of bindings) {
      qc.prefetchQuery({
        queryKey: ['diff', storyKey, b.project_id],
        queryFn: () => diffApi.get(storyKey, b.project_id),
        staleTime: 2 * 60 * 1000,
      })
    }
  }, [qc, storyKey, storyCtx?.story_projects])

  const [planTriggered, setPlanTriggered] = useState(false)
  // Re-entry guard for the SSE effect — a ref avoids setState-in-effect and,
  // unlike state, doesn't sit in the dep array (which previously caused the
  // effect to re-run and tear down the EventSource right after opening it).
  const planTriggeredRef = useRef(false)
  const [streamingActions, setStreamingActions] = useState<AgentAction[]>([])

  const { data: planData } = useQuery({
    queryKey: ['plan', storyKey],
    queryFn: () => planApi.get(storyKey),
    // /plan 现在也回 stages 进度 + stage_gate(确认闸卡片),active/paused 期间也要拉。
    // planning 阶段才需要高频轮询(SSE 流式规划);执行期降到 10s 够看进度/gate。
    enabled: !!detail && ['planning', 'active', 'paused', 'implementing'].includes(detail.status),
    refetchInterval: planTriggered ? false : (detail?.status === 'planning' ? 5000 : 10000),
  })

  // SSE stream for Agent planning
  useEffect(() => {
    if (detail?.status !== 'planning') return
    // BUG #5:"start 即规划"(#4 决策保留)下,进 effect 时 actions 应为空(SSE 触发后端规划)。
    // 仅当规划已完成(actions 非空且 confirmed=false)时不重复建 SSE;否则建连消费流式。
    const existingActions = planData?.actions
    if (existingActions?.length && planData?.confirmed === false) return
    if (planData?.plan_summary && !planData?.actions) return
    if (planTriggeredRef.current) return
    planTriggeredRef.current = true
    const es = new EventSource(planApi.streamUrl(storyKey))
    // Pauses plan polling once the stream is live; fires async, not in the
    // effect body, so it doesn't trip set-state-in-effect.
    es.onopen = () => setPlanTriggered(true)
    es.onmessage = (e) => {
      try {
        const d = JSON.parse(e.data)
        if (d.type === 'action') {
          setStreamingActions(prev => [...prev, d.action])
        } else if (d.type === 'done') {
          es.close()
          // BUG #1:规划完成后强制刷新 plan + story detail,让确认按钮立即出现(原需手动 F5)。
          qc.invalidateQueries({ queryKey: ['plan', storyKey] })
          qc.invalidateQueries({ queryKey: ['story', storyKey] })
        } else if (d.type === 'error') {
          es.close()
        }
      } catch { /* ignore */ }
    }
    es.onerror = () => { es.close(); planTriggeredRef.current = false; setPlanTriggered(false); qc.invalidateQueries({ queryKey: ['plan', storyKey] }) }
    return () => es.close()
  }, [detail?.status, planData, storyKey, qc])

  const isConfirmed = planData?.confirmed ?? false
  // resolvedActions = streaming(规划中实时) / planData.actions(已落 DB) +
  // 本地 adapterOverrides(乐观更新:PATCH 成功到 DB 前 UI 先翻)。
  const baseActions: AgentAction[] = streamingActions.length > 0 ? streamingActions : (planData?.actions ?? [])
  const [adapterOverrides, setAdapterOverrides] = useState<Record<number, string>>({})
  const resolvedActions: AgentAction[] = baseActions.map((a, i) =>
    adapterOverrides[i] ? { ...a, adapter: adapterOverrides[i] } : a
  )

  if (!storyKey) return <div className="loading">无效的 Story Key</div>
  if (!detail) return <div className="loading">加载中...</div>

  const actions = ACTIONS[detail.status] || []

  // PRD 路径(sidebar「打开 PRD」用 + 概览头部)。从 context bundle 的 documents 里取。
  const prdPath = (storyCtx?.documents ?? []).find((d) => d.kind === 'prd')?.ref || ''

  // single-pass 等 profile 创建即 active,但执行从未触发(无 _active_execution)。
  // overview 对这种 story 显示「开始执行」按钮(调 /advance 首次启动)。
  // 已在跑的(有 _active_execution)不显示,避免重复启动。
  let ctx: Record<string, unknown>
  try {
    ctx = JSON.parse(detail.contextJson || '{}')
  } catch {
    ctx = {}
  }
  const neverStarted = !ctx._active_execution

  // 下拉即改即生效:onChange 立即 PATCH 到 DB,本地乐观翻 UI。
  // PATCH 成功后 invalidate plan query,DB 回的 actions 会覆盖本地 overrides
  // (值一致,无缝);失败则弹错并保持原值(下一次 onChange 会再覆盖)。
  function handleActionAdapterChange(index: number, adapter: string) {
    const stage = baseActions[index]?.stage
    if (!stage) return
    // 乐观更新:UI 先翻
    setAdapterOverrides(prev => ({ ...prev, [index]: adapter }))
    planApi
      .updateAdapter(storyKey, stage, adapter)
      .then(() => {
        // DB 已是最新,拉一次 plan 让 query cache 同步(下次 invalidate 时
        // baseActions 会带新 adapter,本地 overrides 自然失效)
        qc.invalidateQueries({ queryKey: ['plan', storyKey] })
      })
      .catch(async (e) => {
        // 回滚乐观更新
        setAdapterOverrides(prev => {
          const next = { ...prev }
          delete next[index]
          return next
        })
        const detail = e instanceof Error ? e.message : String(e)
        alert(`CLI 类型更新失败: ${detail}`)
      })
  }

  async function handleConfirmPlan() {
    // adapter 已经在每次下拉时通过 PATCH 落到 DB 了,confirm 不用再传 actions。
    // 这里只负责翻状态 + 启动执行。
    const r = await fetch(`/api/story/${storyKey}/plan/confirm`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    })
    if (r.ok) {
      refetch()
      scrollToTerminal()
    } else {
      alert(`确认失败: ${(await r.json()).detail || '未知错误'}`)
    }
  }

  async function handleAdvanceLifecycle() {
    // BUG #20: story_state advance(开发→测试→上线)成功后滚动到终端,与 #2 同类。
    // 原 OverviewTab 裸调 advanceLifecycle 不刷新,用户看不到执行。
    const r = await fetch(`/api/story/${storyKey}/lifecycle/advance`, { method: 'POST' })
    if (r.ok) {
      refetch()
      qc.invalidateQueries({ queryKey: ['plan', storyKey] })
      qc.invalidateQueries({ queryKey: ['sessions', storyKey] })
      scrollToTerminal()
    } else {
      alert(`推进失败: ${(await r.json()).detail || '未知错误'}`)
    }
  }

  async function handleStart() {
    // single-pass 等 profile「开始执行」:active 但从未启动 → /advance 首次
    // start_story_async。成功后滚动到终端看 claude 启动(终端在概览底部)。
    const r = await fetch(`/api/story/${storyKey}/advance`, { method: 'PUT' })
    if (r.ok) {
      refetch()
      qc.invalidateQueries({ queryKey: ['plan', storyKey] })
      qc.invalidateQueries({ queryKey: ['sessions', storyKey] })
      scrollToTerminal()
    } else {
      alert(`启动失败: ${(await r.json()).detail || '未知错误'}`)
    }
  }

  // 终端已并入概览底部;执行类操作(确认规划/推进/开始)成功后滚动到终端区,
  // 让用户立刻看到 CLI 输出,不再切 tab。
  function scrollToTerminal() {
    requestAnimationFrame(() => {
      document.getElementById('overview-terminal')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    })
  }

  async function handleRegeneratePlan() {
    planTriggeredRef.current = false
    setPlanTriggered(false)
    setStreamingActions([])
    try {
      await planApi.regenerate(storyKey)
    } catch {
      /* ignore — invalidate will refetch below */
    }
    qc.invalidateQueries({ queryKey: ['plan', storyKey] })
  }

  async function handleAction(action: ActionButton) {
    if (action.confirm && !window.confirm(action.confirm)) return
    let url = `/api/story/${storyKey}`
    if (action.path) url += action.path
    if (await apiAction(action.method, url)) {
      if (action.method === 'DELETE') navigate('/')
      else { refetch(); qc.invalidateQueries({ queryKey: ['timeline', storyKey] }) }
    }
  }

  async function handleResolve() {
    if (!window.confirm('确认 bug 已修复？会更新 TAPD + 本地状态。')) return
    const r = await fetch(`/api/story/${storyKey}/resolve`, { method: 'POST' })
    if (r.ok) {
      const body = await r.json()
      if (!body.has_bugfix_report) alert('⚠ 未发现 bugfix-report 证据，建议补记后再 resolve')
      refetch()
    } else {
      alert('resolve 失败')
    }
  }

  async function handleArchive() {
    if (!window.confirm('确定归档此 Story？归档后会从默认列表中隐藏，但不会被删除。')) return
    const r = await fetch(`/api/story/${storyKey}/archive`, { method: 'PUT' })
    if (r.ok) {
      refetch()
      qc.invalidateQueries({ queryKey: ['stories'] })
    } else {
      alert('归档失败')
    }
  }

  return (
    <div className="story-detail-page-v2">
      <div className="sdpv2-body">
        <StorySidebar
          storyKey={storyKey}
          modules={MODULES}
          activeModule={validTab}
          onModuleChange={setActiveTab}
          onNavigate={handleNavigate}
          onArchive={handleArchive}
          onBack={() => navigate('/')}
          prdPath={prdPath}
          onAdvance={handleAdvanceLifecycle}
        />
        <div className="sdpv2-content">
          <ClarifyDialog storyKey={storyKey} status={detail.status} headless={detail.headless} />
          {validTab === 'overview' && (
            <OverviewTab
              storyKey={storyKey}
              detail={detail}
              resolvedActions={resolvedActions}
              isConfirmed={isConfirmed}
              planData={planData}
              onConfirmPlan={handleConfirmPlan}
              onRegeneratePlan={handleRegeneratePlan}
              onAction={handleAction}
              actions={actions}
              onActionAdapterChange={handleActionAdapterChange}
              neverStarted={neverStarted}
              onStart={handleStart}
              onResolve={detail.tapdType === 'bug' ? handleResolve : undefined}
            />
          )}
          {validTab === 'code' && <CodeChangesTab storyKey={storyKey} />}
          {validTab === 'docs' && <DocsTab storyKey={storyKey} />}
        </div>
      </div>
    </div>
  )
}
