import { useState, useEffect } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useParams, useNavigate, useSearchParams } from 'react-router-dom'
import { storyApi, apiAction, planApi } from '../api/client'
import StorySidebar from '../components/StorySidebar'
import OverviewTab from '../components/OverviewTab'
import TerminalTab from '../components/TerminalTab'
import './StoryDetailPage.css'

interface AgentAction {
  action: 'launch' | 'skip'
  adapter?: string
  stage?: string
  focus?: string
  done_file?: string
  reason?: string
}

const MODULES = [
  { id: 'overview', icon: '📊', label: '概览' },
  { id: 'code', icon: '💻', label: '代码变更' },
  { id: 'loop', icon: '🔁', label: '对抗循环' },
  { id: 'test', icon: '🧪', label: '测试' },
  { id: 'quality', icon: '🛡', label: '质量 & Gate' },
  { id: 'terminal', icon: '💻', label: '终端' },
]

const ACTIONS: Record<string, any[]> = {
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

  const [planTriggered, setPlanTriggered] = useState(false)
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
    if (planTriggered) return
    setPlanTriggered(true)
    const es = new EventSource(planApi.streamUrl(storyKey))
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
    es.onerror = () => { es.close(); setPlanTriggered(false); qc.invalidateQueries({ queryKey: ['plan', storyKey] }) }
    return () => es.close()
  }, [detail?.status, planData, planTriggered, storyKey, qc])

  const resolvedActions: AgentAction[] = streamingActions.length > 0 ? streamingActions : (planData?.actions ?? [])
  const isConfirmed = planData?.confirmed ?? false

  if (!storyKey) return <div className="loading">无效的 Story Key</div>
  if (!detail) return <div className="loading">加载中...</div>

  const actions = ACTIONS[detail.status] || []

  async function handleConfirmPlan() {
    const r = await fetch(`/api/story/${storyKey}/plan/confirm`, { method: 'POST' })
    if (r.ok) refetch()
    else alert(`确认失败: ${(await r.json()).detail || '未知错误'}`)
  }

  async function handleRegeneratePlan() {
    setPlanTriggered(false); setStreamingActions([])
    try { await planApi.regenerate(storyKey) } catch {}
    qc.invalidateQueries({ queryKey: ['plan', storyKey] })
  }

  async function handleAction(action: any) {
    if (action.confirm && !window.confirm(action.confirm)) return
    let url = `/api/story/${storyKey}`
    if (action.path === '/skip/{stage}') url += `/skip/${detail?.currentStage}`
    else if (action.path) url += action.path
    if (await apiAction(action.method, url)) {
      if (action.method === 'DELETE') navigate('/')
      else { refetch(); qc.invalidateQueries({ queryKey: ['timeline', storyKey] }) }
    }
  }

  return (
    <div className="story-detail-page-v2">
      <div className="sdpv2-topbar">
        <button className="btn btn-back" onClick={() => navigate('/')}>← 返回</button>
        {detail.lastError && <span className="sdpv2-error-badge" title={detail.lastError}>⚠ {detail.lastError}</span>}
      </div>
      <div className="sdpv2-body">
        <StorySidebar
          storyKey={storyKey}
          storyTitle={detail.title || storyKey}
          storyStatus={detail.status}
          modules={MODULES}
          activeModule={activeTab}
          onModuleChange={setActiveTab}
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
          {activeTab === 'code' && (
            <div className="tab-placeholder">💻 代码变更 — 即将实现</div>
          )}
          {activeTab === 'loop' && (
            <div className="tab-placeholder">🔁 对抗循环 — 即将实现</div>
          )}
          {activeTab === 'test' && (
            <div className="tab-placeholder">🧪 测试 — 即将实现</div>
          )}
          {activeTab === 'quality' && (
            <div className="tab-placeholder">🛡 质量 & Gate — 即将实现</div>
          )}
          {activeTab === 'terminal' && (
            <TerminalTab storyKey={storyKey} status={detail.status} />
          )}
        </div>
      </div>
    </div>
  )
}
