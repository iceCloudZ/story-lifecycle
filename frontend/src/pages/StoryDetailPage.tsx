import { useState, useEffect } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useParams, useNavigate } from 'react-router-dom'
import { storyApi, apiAction, planApi } from '../api/client'
import TerminalPanel from '../components/TerminalPanel'
import ActionCard from '../components/ActionCard'
import './StoryDetailPage.css'

/* ---- Action type from Agent ---- */
interface AgentAction {
  action: 'launch' | 'skip'
  adapter?: string
  stage?: string
  focus?: string
  done_file?: string
  reason?: string
}

const ACTIONS: Record<string, { label: string; method: string; path: string; confirm?: string; variant?: string }[]> = {
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

  const { data: detail, refetch } = useQuery({
    queryKey: ['story', storyKey],
    queryFn: () => storyApi.get(storyKey),
    refetchInterval: 5000,
  })

  const { data: timeline } = useQuery({
    queryKey: ['timeline', storyKey],
    queryFn: () => storyApi.timeline(storyKey),
    enabled: !!detail,
  })

  const { data: gateHistory } = useQuery({
    queryKey: ['gateHistory', storyKey],
    queryFn: () => storyApi.gateHistory(storyKey),
    enabled: !!detail,
  })

  const { data: loopTrace } = useQuery({
    queryKey: ['loopTrace', storyKey],
    queryFn: () => storyApi.loopTrace(storyKey),
    enabled: !!detail,
  })

  const [findingsFilter, setFindingsFilter] = useState({ status: '', minSeverity: '' })
  const { data: findingsData } = useQuery({
    queryKey: ['findings', storyKey, findingsFilter],
    queryFn: () => storyApi.findings(storyKey, findingsFilter.status, findingsFilter.minSeverity),
    enabled: !!detail,
  })

  const { data: depGraph } = useQuery({
    queryKey: ['depGraph', storyKey],
    queryFn: () => storyApi.dependencyGraph(storyKey),
    enabled: !!detail,
  })

  /* ---- Agent plan state ---- */
  const [planTriggered, setPlanTriggered] = useState(false)
  const [streamingActions, setStreamingActions] = useState<AgentAction[]>([])
  const [planError, setPlanError] = useState('')

  const { data: planData } = useQuery({
    queryKey: ['plan', storyKey],
    queryFn: () => planApi.get(storyKey),
    enabled: !!detail && detail.status === 'planning',
    refetchInterval: planTriggered ? false : 5000,
  })

  /* SSE: stream Agent actions */
  useEffect(() => {
    if (detail?.status !== 'planning') return
    // Don't re-trigger if we already have actions (from DB or SSE)
    const existingActions = planData?.actions
    if (existingActions?.length) return
    if (planData?.plan_summary && !planData?.actions) return // legacy mode
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
          setPlanError(d.message)
          es.close()
        }
      } catch { /* ignore parse errors */ }
    }

    es.onerror = () => {
      es.close()
      setPlanTriggered(false)
      qc.invalidateQueries({ queryKey: ['plan', storyKey] })
    }
    return () => es.close()
  }, [detail?.status, planData, planTriggered, storyKey, qc])

  /* Resolve actions: prefer streaming, fall back to DB, then legacy */
  const resolvedActions: AgentAction[] =
    streamingActions.length > 0
      ? streamingActions
      : (planData?.actions ?? [])

  const isLegacyPlan = !resolvedActions.length && !!planData?.plan_summary
  const isConfirmed = planData?.confirmed ?? false

  /* ---- Wait question (human-in-the-loop) ---- */
  const [waitQuestion, setWaitQuestion] = useState<any>(null)

  useEffect(() => {
    if (detail?.status !== 'active') return
    let cancelled = false
    const poll = async () => {
      while (!cancelled) {
        try {
          const data = await planApi.waitQuestion(storyKey)
          if (!cancelled && data.waiting) {
            setWaitQuestion(data.question)
            return // stop polling once question found
          }
        } catch { /* ignore */ }
        await new Promise(r => setTimeout(r, 5000))
      }
    }
    poll()
    return () => { cancelled = true }
  }, [detail?.status, storyKey])

  async function handleAnswer(answer: string) {
    await planApi.answer(storyKey, answer)
    setWaitQuestion(null)
  }

  if (!storyKey) return <div className="loading">无效的 Story Key</div>
  if (!detail) return <div className="loading">加载中...</div>

  const actions = ACTIONS[detail.status] || []

  async function handleConfirmPlan() {
    const r = await fetch(`/api/story/${storyKey}/plan/confirm`, { method: 'POST' })
    if (r.ok) refetch()
    else { const e = await r.json(); alert(`确认失败: ${e.detail || '未知错误'}`) }
  }

  async function handleRegeneratePlan() {
    setPlanTriggered(false)
    setStreamingActions([])
    setPlanError('')
    try {
      await planApi.regenerate(storyKey)
    } catch { /* fallback: next render triggers SSE */ }
    qc.invalidateQueries({ queryKey: ['plan', storyKey] })
  }

  async function handleAction(action: (typeof actions)[0]) {
    if (action.confirm && !window.confirm(action.confirm)) return
    let url = `/api/story/${storyKey}`
    if (action.path === '/skip/{stage}') {
      url += `/skip/${detail?.currentStage}`
    } else if (action.path) {
      url += action.path
    }
    const ok = await apiAction(action.method, url)
    if (ok) {
      if (action.method === 'DELETE') {
        navigate('/')
      } else {
        refetch()
        qc.invalidateQueries({ queryKey: ['timeline', storyKey] })
        qc.invalidateQueries({ queryKey: ['gateHistory', storyKey] })
        qc.invalidateQueries({ queryKey: ['findings', storyKey] })
      }
    }
  }

  return (
    <div className="story-detail-page">
      <div className="sdp-header">
        <button className="btn btn-back" onClick={() => navigate('/')}>← 返回</button>
        <span className="sdp-key">{detail.storyKey}</span>
        <span className={`badge badge-${detail.status}`}>{detail.status}</span>
        <span className="sdp-stage">阶段: {detail.currentStage}</span>
      </div>

      {detail.lastError && (
        <div className="sdp-error">{detail.lastError}</div>
      )}

      <div className="sdp-info">
        <div><span className="label">标题</span>{detail.title || '-'}</div>
        <div><span className="label">Profile</span>{detail.profile}</div>
        <div><span className="label">重试</span>{detail.executionCount}</div>
        <div><span className="label">更新</span>{detail.updatedAt}</div>
        {detail.parentKey && <div><span className="label">父 Story</span>{detail.parentKey}</div>}
      </div>

      <div className="sdp-actions">
        {detail.status === 'planning' && !isConfirmed && resolvedActions.length > 0 && (
          <button className="btn btn-primary" onClick={handleConfirmPlan}>
            ✅ 确认并执行 ({resolvedActions.filter(a => a.action === 'launch').length} 步)
          </button>
        )}
        {detail.status === 'planning' && (
          <button className="btn" onClick={handleRegeneratePlan}>
            🔄 重新规划
          </button>
        )}
        {actions.map((a) => (
          <button
            key={a.label}
            className={`btn action-btn ${a.variant === 'danger' ? 'btn-danger' : ''} ${a.variant === 'primary' ? 'btn-primary' : ''}`}
            onClick={() => handleAction(a)}
          >
            {a.label}
          </button>
        ))}
      </div>

      {/* ---- Agent Action Cards (planning phase) ---- */}
      {detail.status === 'planning' && (
        <section className="sdp-section plan-section">
          <h3>🤖 Agent 规划</h3>

          {/* Structured action cards */}
          {resolvedActions.length > 0 ? (
            <div className="action-cards">
              {resolvedActions.map((a, i) => (
                <ActionCard key={i} action={a} index={i} />
              ))}
            </div>
          ) : planError ? (
            <div className="plan-error">❌ {planError}</div>
          ) : isLegacyPlan ? (
            /* Legacy fallback: text-based plan */
            <>
              {planData.plan_summary && (
                <div className="plan-summary">{planData.plan_summary}</div>
              )}
              {planData.plan_content && (
                <pre className="plan-content">{planData.plan_content}</pre>
              )}
            </>
          ) : (
            <div className="plan-loading">
              <span className="plan-spinner" /> Agent 正在规划...
            </div>
          )}
        </section>
      )}

      {/* ---- Wait Question (human-in-the-loop) ---- */}
      {waitQuestion && (
        <section className="sdp-section wait-section">
          <h3>❓ CLI 等待确认</h3>
          <div className="wait-question">{waitQuestion.question}</div>
          {waitQuestion.context && (
            <div className="wait-context">{waitQuestion.context}</div>
          )}
          {waitQuestion.options ? (
            <div className="wait-options">
              {waitQuestion.options.map((opt: string, i: number) => (
                <button key={i} className="btn btn-primary" onClick={() => handleAnswer(opt)}>
                  {opt}
                </button>
              ))}
            </div>
          ) : (
            <div className="wait-input">
              <input
                type="text"
                placeholder="输入回答..."
                onKeyDown={(e) => {
                  if (e.key === 'Enter') handleAnswer((e.target as HTMLInputElement).value)
                }}
              />
              <button className="btn btn-primary" onClick={() => {
                const input = document.querySelector('.wait-input input') as HTMLInputElement
                if (input?.value) handleAnswer(input.value)
              }}>提交</button>
            </div>
          )}
        </section>
      )}

      {/* Horizontal Timeline */}
      {timeline && timeline.stages?.length > 0 && (
        <section className="sdp-section">
          <h3>阶段时间线</h3>
          <HorizontalTimeline stages={timeline.stages} />
        </section>
      )}

      {/* Gate History - expandable panels */}
      {gateHistory && gateHistory.decisions?.length > 0 && (
        <section className="sdp-section">
          <h3>Gate 决策历史 ({gateHistory.decisions.length})</h3>
          <GatePanels decisions={gateHistory.decisions} />
        </section>
      )}

      {/* Loop Trace - diff view */}
      {loopTrace && (loopTrace.plan_loop?.rounds?.length > 0 || loopTrace.code_loop?.rounds?.length > 0) && (
        <section className="sdp-section">
          <h3>对抗循环轨迹</h3>
          <LoopTracePanel loopTrace={loopTrace} />
        </section>
      )}

      {/* Findings - with filters */}
      <section className="sdp-section">
        <h3>Findings {findingsData?.findings ? `(${findingsData.findings.length})` : ''}</h3>
        <div className="findings-filters">
          <select
            value={findingsFilter.status}
            onChange={(e) => setFindingsFilter((f) => ({ ...f, status: e.target.value }))}
          >
            <option value="">全部状态</option>
            <option value="open">Open</option>
            <option value="resolved">Resolved</option>
            <option value="dismissed">Dismissed</option>
          </select>
          <select
            value={findingsFilter.minSeverity}
            onChange={(e) => setFindingsFilter((f) => ({ ...f, minSeverity: e.target.value }))}
          >
            <option value="">全部严重度</option>
            <option value="high">High+</option>
            <option value="medium">Medium+</option>
            <option value="low">Low+</option>
          </select>
        </div>
        {findingsData?.findings?.length > 0 ? (
          <FindingsList findings={findingsData.findings} />
        ) : (
          <p className="sdp-empty">无匹配的 Findings</p>
        )}
      </section>

      {/* Sub-stories / Dependency Graph */}
      {depGraph && depGraph.nodes?.length > 1 && (
        <section className="sdp-section">
          <h3>子 Story 依赖图</h3>
          <DependencyGraph nodes={depGraph.nodes} edges={depGraph.edges} />
        </section>
      )}

      {/* Terminal */}
      <section className="sdp-section">
        <h3>终端</h3>
        <TerminalPanel storyKey={storyKey} />
      </section>
    </div>
  )
}


/* ---- Horizontal Timeline ---- */
function HorizontalTimeline({ stages }: { stages: any[] }) {
  return (
    <div className="h-timeline">
      {stages.map((s: any, i: number) => (
        <div key={s.stage} className={`ht-step ht-${s.status || 'pending'}`}>
          <div className="ht-connector">
            {i > 0 && <div className="ht-line" />}
            <div className="ht-dot" />
          </div>
          <div className="ht-content">
            <div className="ht-stage-name">{s.stage}</div>
            <div className="ht-status-text">{s.status || 'pending'}</div>
            {s.duration_ms != null && (
              <div className="ht-duration">{(s.duration_ms / 1000).toFixed(1)}s</div>
            )}
            {s.trajectory_score != null && (
              <div className="ht-score">评分: {s.trajectory_score}</div>
            )}
            {s.loop_rounds > 0 && (
              <div className="ht-rounds">循环: {s.loop_rounds}轮</div>
            )}
            {s.plan_summary && (
              <div className="ht-summary" title={s.plan_summary}>
                {s.plan_summary.length > 60 ? s.plan_summary.slice(0, 60) + '...' : s.plan_summary}
              </div>
            )}
          </div>
        </div>
      ))}
    </div>
  )
}

/* ---- Gate Panels (expandable) ---- */
function GatePanels({ decisions }: { decisions: any[] }) {
  const [expanded, setExpanded] = useState<number | null>(null)
  return (
    <div className="gate-list">
      {decisions.map((d: any, i: number) => (
        <div key={i} className="gate-item" onClick={() => setExpanded(expanded === i ? null : i)}>
          <div className="gate-top">
            <span className={`gate-decision gate-${d.decision}`}>{d.decision}</span>
            <span className="gate-reason">{d.reason_code}</span>
            <span className="gate-stage">{d.stage}</span>
            <span className="gate-expand">{expanded === i ? '▼' : '▶'}</span>
          </div>
          {expanded === i && (
            <div className="gate-detail">
              {d.human_message && <div className="gate-msg">{d.human_message}</div>}
              {d.evidence && (
                <div className="gate-evidence">
                  <strong>Evidence:</strong>
                  <pre>{typeof d.evidence === 'string' ? d.evidence : JSON.stringify(d.evidence, null, 2)}</pre>
                </div>
              )}
              {d.available_actions && (
                <div className="gate-actions-hint">
                  可用操作: {d.available_actions.join(', ')}
                </div>
              )}
            </div>
          )}
        </div>
      ))}
    </div>
  )
}

/* ---- Loop Trace Panel ---- */
function LoopTracePanel({ loopTrace }: { loopTrace: any }) {
  return (
    <div className="loop-trace">
      {loopTrace.plan_loop?.rounds?.length > 0 && (
        <div className="loop-section">
          <h4>Plan Loop ({loopTrace.plan_loop.rounds.length} 轮)</h4>
          {loopTrace.plan_loop.rounds.map((r: any, i: number) => (
            <LoopRound key={i} round={r} index={i} />
          ))}
        </div>
      )}
      {loopTrace.code_loop?.rounds?.length > 0 && (
        <div className="loop-section">
          <h4>Code Review Loop ({loopTrace.code_loop.rounds.length} 轮)</h4>
          {loopTrace.code_loop.rounds.map((r: any, i: number) => (
            <LoopRound key={i} round={r} index={i} />
          ))}
        </div>
      )}
    </div>
  )
}

function LoopRound({ round, index }: { round: any; index: number }) {
  const [expanded, setExpanded] = useState(false)
  return (
    <div className="loop-round-card" onClick={() => setExpanded(!expanded)}>
      <div className="lr-header">
        <span className="lr-num">Round {index + 1}</span>
        <span className={`lr-decision lr-${round.loop_decision}`}>{round.loop_decision}</span>
        {round.trajectory_score != null && <span className="lr-score">评分: {round.trajectory_score}</span>}
        <span className="gate-expand">{expanded ? '▼' : '▶'}</span>
      </div>
      {expanded && (
        <div className="lr-detail">
          {round.quality && <div><span className="label">质量</span>{round.quality}</div>}
          {round.reviewer_feedback && (
            <div className="lr-feedback">
              <span className="label">反馈</span>
              <pre>{round.reviewer_feedback}</pre>
            </div>
          )}
          {round.optimizer_response && (
            <div className="lr-response">
              <span className="label">优化响应</span>
              <pre>{round.optimizer_response}</pre>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

/* ---- Findings List ---- */
function FindingsList({ findings }: { findings: any[] }) {
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const sorted = [...findings].sort((a, b) => {
    const order: Record<string, number> = { high: 0, medium: 1, low: 2 }
    return (order[a.severity] ?? 3) - (order[b.severity] ?? 3)
  })
  return (
    <div className="findings-list">
      {sorted.map((f: any) => (
        <div
          key={f.id}
          className={`finding-item severity-${f.severity}`}
          onClick={() => setExpandedId(expandedId === f.id ? null : f.id)}
        >
          <span className="finding-sev">[{f.severity.toUpperCase()}]</span>
          <span className="finding-cat">{f.category}</span>
          <span className="finding-desc">
            {expandedId === f.id ? (f.description ?? '--') : (f.description?.length > 80 ? f.description.slice(0, 80) + '...' : (f.description ?? '--'))}
          </span>
          <span className="finding-status">{f.status}</span>
        </div>
      ))}
    </div>
  )
}

/* ---- Dependency Graph (simple topological) ---- */
function DependencyGraph({ nodes, edges }: { nodes: any[]; edges: any[] }) {
  return (
    <div className="dep-graph">
      <div className="dep-nodes">
        {nodes.map((n: any) => (
          <div key={n.key} className={`dep-node dep-${n.status}`}>
            <span className="dep-key">{n.key}</span>
            <span className="dep-status">{n.status}</span>
            <span className="dep-stage">{n.stage}</span>
          </div>
        ))}
      </div>
      {edges?.length > 0 && (
        <div className="dep-edges">
          {edges.map((e: any, i: number) => (
            <div key={i} className="dep-edge">{e.from} → {e.to}</div>
          ))}
        </div>
      )}
    </div>
  )
}
