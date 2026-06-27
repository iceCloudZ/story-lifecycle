import { useState, useEffect, useRef } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useParams, useNavigate, useSearchParams } from 'react-router-dom'
import { storyApi, apiAction, planApi } from '../api/client'
import type { AgentAction, ActionButton } from '../api/client'
import StorySidebar from '../components/StorySidebar'
import OverviewTab from '../components/OverviewTab'
import CodeChangesTab from '../components/CodeChangesTab'
import AdversarialLoopTab from '../components/AdversarialLoopTab'
import TestTab from '../components/TestTab'
import QualityGateTab from '../components/QualityGateTab'
import TerminalTab from '../components/TerminalTab'
import ContextTab from '../components/ContextTab'
import BugsTab from '../components/BugsTab'
import './StoryDetailPage.css'

const MODULES = [
  { id: 'overview', icon: '📊', label: '概览' },
  { id: 'bugs', icon: '🐛', label: '缺陷' },
  { id: 'code', icon: '💻', label: '代码变更' },
  { id: 'loop', icon: '🔁', label: '对抗循环' },
  { id: 'test', icon: '🧪', label: '测试' },
  { id: 'quality', icon: '🛡', label: '质量 & Gate' },
  { id: 'context', icon: '📄', label: '上下文' },
  { id: 'terminal', icon: '💻', label: '终端' },
]

const ACTIONS: Record<string, ActionButton[]> = {
  planning: [
    { label: '终止', method: 'POST', path: '/abort', confirm: '确定终止此 Story？', variant: 'danger' },
  ],
  active: [
    { label: '跳过阶段', method: 'PUT', path: '/skip/{stage}' },
    { label: '终止', method: 'POST', path: '/abort', confirm: '确定终止此 Story？', variant: 'danger' },
  ],
  paused: [
    { label: '继续执行', method: 'PUT', path: '/advance', variant: 'primary' },
    { label: '跳过阶段', method: 'PUT', path: '/skip/{stage}' },
    { label: '终止', method: 'POST', path: '/abort', confirm: '确定终止此 Story？', variant: 'danger' },
  ],
  blocked: [
    { label: '重试', method: 'PUT', path: '/advance', variant: 'primary' },
    { label: '终止', method: 'POST', path: '/abort', confirm: '确定终止此 Story？', variant: 'danger' },
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
    enabled: !!detail && detail.status === 'planning',
    refetchInterval: planTriggered ? false : 5000,
  })

  // SSE stream for Agent planning
  useEffect(() => {
    if (detail?.status !== 'planning') return
    const existingActions = planData?.actions
    if (existingActions?.length) return
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
          qc.invalidateQueries({ queryKey: ['plan', storyKey] })
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

  if (!storyKey) return <div className="loading">无效的 Story Key</div>
  if (!detail) return <div className="loading">加载中...</div>

  const actions = ACTIONS[detail.status] || []

  async function handleConfirmPlan() {
    const r = await fetch(`/api/story/${storyKey}/plan/confirm`, { method: 'POST' })
    if (r.ok) {
      refetch()
      setActiveTab('terminal')
    } else {
      alert(`确认失败: ${(await r.json()).detail || '未知错误'}`)
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
    if (action.path === '/skip/{stage}') url += `/skip/${detail?.currentStage}`
    else if (action.path) url += action.path
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
          {activeTab === 'overview' && (
            <OverviewTab
              storyKey={storyKey}
              detail={detail}
              resolvedActions={resolvedActions}
              isConfirmed={isConfirmed}
              onConfirmPlan={handleConfirmPlan}
              onRegeneratePlan={handleRegeneratePlan}
              onAction={handleAction}
              actions={actions}
              onTabChange={setActiveTab}
            />
          )}
          {activeTab === 'code' && <CodeChangesTab storyKey={storyKey} />}
          {activeTab === 'loop' && <AdversarialLoopTab storyKey={storyKey} />}
          {activeTab === 'test' && <TestTab storyKey={storyKey} />}
          {activeTab === 'quality' && <QualityGateTab storyKey={storyKey} />}
          {activeTab === 'bugs' && <BugsTab storyKey={storyKey} />}
          {activeTab === 'context' && <ContextTab storyKey={storyKey} />}
          {activeTab === 'terminal' && (
            <TerminalTab storyKey={storyKey} status={detail.status} />
          )}
        </div>
      </div>
    </div>
  )
}
