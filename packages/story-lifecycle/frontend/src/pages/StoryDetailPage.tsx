import { useState, useEffect, useRef } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useParams, useNavigate, useSearchParams } from 'react-router-dom'
import { storyApi, apiAction, planApi } from '../api/client'
import type { AgentAction, ActionButton } from '../api/client'
import StorySidebar from '../components/StorySidebar'
import OverviewTab from '../components/OverviewTab'
import CodeChangesTab from '../components/CodeChangesTab'
import TestTab from '../components/TestTab'
import TerminalTab from '../components/TerminalTab'
import BugsTab from '../components/BugsTab'
import LlmAuditTab from '../components/LlmAuditTab'
import DocsTab from '../components/DocsTab'
import ClarifyDialog from '../components/ClarifyDialog'
import './StoryDetailPage.css'

// Visible modules for the semi-automatic workflow. Terminal 放回 sidebar:
// handleConfirmPlan / handleAdvanceLifecycle 都 setActiveTab('terminal'),
// 必须让用户能从 sidebar 够得到它。loop/quality/context 已移除渲染分支。
const MODULES = [
  { id: 'overview', icon: '📊', label: '概览' },
  { id: 'terminal', icon: '💻', label: '终端' },
  { id: 'code', icon: '📦', label: '代码变更' },
  { id: 'test', icon: '🧪', label: '测试' },
  { id: 'docs', icon: '📄', label: '文档' },
  { id: 'llm-audit', icon: '🔍', label: 'LLM 审计' },
  { id: 'bugs', icon: '🐛', label: '缺陷' },
]

const ACTIONS: Record<string, ActionButton[]> = {
  planning: [],
  active: [
    { label: '紧急停止', method: 'POST', path: '/emergency-stop', confirm: '立即杀掉运行中的 claude 进程并暂停（可恢复）。确定？', variant: 'danger' },
  ],
  paused: [
    { label: '继续执行', method: 'PUT', path: '/advance', variant: 'primary' },
  ],
  blocked: [
    { label: '重试', method: 'PUT', path: '/advance', variant: 'primary' },
  ],
  failed: [
    { label: '删除', method: 'DELETE', path: '', confirm: '确定删除？不可恢复。', variant: 'danger' },
  ],
  completed: [
    { label: '删除', method: 'DELETE', path: '', confirm: '确定删除？不可恢复。', variant: 'danger' },
  ],
  aborted: [
    { label: '删除', method: 'DELETE', path: '', confirm: '确定删除？不可恢复。', variant: 'danger' },
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

  const resolvedActions: AgentAction[] = streamingActions.length > 0 ? streamingActions : (planData?.actions ?? [])
  const isConfirmed = planData?.confirmed ?? false
  // per-stage adapter 覆盖:用户在 ActionCard 下拉改过的 adapter(stage→adapter)。
  // 初始空 = 用 LLM 规划的默认值;改过才有值。confirm 时传给后端覆盖 _agent_actions。
  const [adapterOverrides, setAdapterOverrides] = useState<Record<number, string>>({})

  if (!storyKey) return <div className="loading">无效的 Story Key</div>
  if (!detail) return <div className="loading">加载中...</div>

  const actions = ACTIONS[detail.status] || []

  function handleActionAdapterChange(index: number, adapter: string) {
    setAdapterOverrides(prev => ({ ...prev, [index]: adapter }))
  }

  async function handleConfirmPlan() {
    // 把用户改过的 adapter 跟 resolvedActions 合并,传给后端覆盖 _agent_actions。
    const actionOverrides = resolvedActions.map((a, i) => ({
      stage: a.stage,
      adapter: adapterOverrides[i] ?? a.adapter,
    }))
    const r = await fetch(`/api/story/${storyKey}/plan/confirm`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ actions: actionOverrides }),
    })
    if (r.ok) {
      refetch()
      setActiveTab('terminal')
    } else {
      alert(`确认失败: ${(await r.json()).detail || '未知错误'}`)
    }
  }

  async function handleAdvanceLifecycle() {
    // BUG #20: story_state advance(开发→测试→上线)成功后跳终端,与 #2 同类。
    // 原 OverviewTab 裸调 advanceLifecycle 不跳 tab 不刷新,用户看不到执行。
    const r = await fetch(`/api/story/${storyKey}/lifecycle/advance`, { method: 'POST' })
    if (r.ok) {
      refetch()
      qc.invalidateQueries({ queryKey: ['plan', storyKey] })
      qc.invalidateQueries({ queryKey: ['sessions', storyKey] })
      setActiveTab('terminal')
    } else {
      alert(`推进失败: ${(await r.json()).detail || '未知错误'}`)
    }
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
      <div className="sdpv2-topbar">
        <button className="btn btn-back" onClick={() => navigate('/')}>← 返回</button>
        {detail.lastError && <span className="sdpv2-error-badge" title={detail.lastError}>⚠ {detail.lastError}</span>}
        {detail.tapdType === 'bug' && (
          <button className="btn btn-primary" onClick={handleResolve}>标记已修复</button>
        )}
      </div>
      <div className="sdpv2-body">
        <StorySidebar
          storyKey={storyKey}
          storyTitle={detail.title || storyKey}
          storyStatus={detail.status}
          modules={MODULES}
          activeModule={activeTab}
          onModuleChange={setActiveTab}
          onArchive={handleArchive}
        />
        <div className="sdpv2-content">
          <ClarifyDialog storyKey={storyKey} status={detail.status} headless={detail.headless} />
          {activeTab === 'overview' && (
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
              onTabChange={setActiveTab}
              onAdvanceLifecycle={handleAdvanceLifecycle}
              onActionAdapterChange={handleActionAdapterChange}
            />
          )}
          {activeTab === 'code' && <CodeChangesTab storyKey={storyKey} />}
          {activeTab === 'llm-audit' && <LlmAuditTab storyKey={storyKey} />}
          {activeTab === 'test' && <TestTab storyKey={storyKey} />}
          {activeTab === 'docs' && <DocsTab storyKey={storyKey} />}
          {activeTab === 'bugs' && <BugsTab storyKey={storyKey} />}
          {activeTab === 'terminal' && (
            <TerminalTab storyKey={storyKey} status={detail.status} />
          )}
        </div>
      </div>
    </div>
  )
}
