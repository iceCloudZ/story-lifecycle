import { useQuery } from '@tanstack/react-query'
import { statsApi } from '../api/client'
import type { Story, AgentAction, ActionButton } from '../api/client'
import StageProgress from './StageProgress'
import ActionCard from './ActionCard'
import ContextTab from './ContextTab'

interface Props {
  storyKey: string
  detail: Story
  resolvedActions: AgentAction[]
  isConfirmed: boolean
  onConfirmPlan: () => void
  onRegeneratePlan: () => void
  onAction: (action: ActionButton) => void
  actions: ActionButton[]
  onTabChange: (tabId: string) => void
}

export default function OverviewTab({
  storyKey, detail, resolvedActions, isConfirmed,
  onConfirmPlan, onRegeneratePlan, onAction, actions, onTabChange,
}: Props) {
  const { data: stats } = useQuery({
    queryKey: ['stats', storyKey],
    queryFn: () => statsApi.get(storyKey),
    enabled: !!detail,
  })

  // Resolve stage list from profile — default to minimal profile stages
  const stages = [
    { name: 'design', status: 'pending' as const },
    { name: 'implement', status: 'pending' as const },
    { name: 'test', status: 'pending' as const },
  ]

  // 启动交互式终端(design HITL):有存活会话直接跳,否则 spawn 一个再跳。
  // 避免重复 spawn(sessions/spawn 总是新建)。spawn 走 claude "query" seed prompt。
  async function startTerminal() {
    try {
      const r = await fetch(`/api/story/${storyKey}/sessions`)
      const data = r.ok ? await r.json() : { sessions: [] }
      const hasAlive = (data.sessions || []).some(
        (s: { status: string }) => s.status === 'running'
      )
      if (!hasAlive) {
        await fetch(`/api/story/${storyKey}/sessions/spawn`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ adapter: 'claude', model: '' }),
        })
      }
    } catch {
      /* 终端 tab 兜底显示「启动终端」按钮 */
    }
    onTabChange('terminal')
  }

  return (
    <div className="tab-content overview-tab">
      {/* Top bar */}
      <div className="ot-header">
        <span className="ot-key">{detail.storyKey}</span>
        <span className="ot-updated">更新: {detail.updatedAt}</span>
      </div>

      {/* Progress bar */}
      <StageProgress stages={stages} currentStage={detail.currentStage} />

      {/* Info cards */}
      <div className="ot-info-grid">
        <div className="ot-info-card">
          <div className="ot-info-label">Profile</div>
          <div className="ot-info-value">{detail.profile}</div>
        </div>
        <div className="ot-info-card">
          <div className="ot-info-label">重试次数</div>
          <div className="ot-info-value">{detail.executionCount} / 3</div>
        </div>
        <div className="ot-info-card">
          <div className="ot-info-label">优先级</div>
          <div className="ot-info-value">{detail.priority || '-'}</div>
        </div>
        <div className="ot-info-card">
          <div className="ot-info-label">来源</div>
          <div className="ot-info-value">{detail.sourceType || '-'}</div>
        </div>
      </div>

      {/* Agent planning area */}
      {detail.status === 'planning' && resolvedActions.length > 0 && (
        <div className="ot-plan-section">
          <h3>🤖 Agent 规划</h3>
          <div className="action-cards">
            {resolvedActions.map((a, i) => (
              <ActionCard key={i} action={a} index={i} />
            ))}
          </div>
        </div>
      )}

      {/* Action buttons */}
      <div className="ot-actions">
        {/* HITL 终端入口:启动交互式 agent 跑当前 stage(design 等),人 watch + Esc + steer */}
        <button className="btn" onClick={startTerminal} title="开交互式终端,claude 自动跑当前阶段,你实时 watch + Esc 打断 + 打字纠偏">
          🖥️ 启动 {detail.currentStage} 终端(HITL)
        </button>
        {detail.status === 'planning' && !isConfirmed && resolvedActions.length > 0 && (
          <>
            <button className="btn btn-primary" onClick={onConfirmPlan}>
              ✅ 确认并执行 ({resolvedActions.filter((a) => a.action === 'launch').length} 步)
            </button>
            <button className="btn" onClick={onRegeneratePlan}>
              🔄 重新规划
            </button>
          </>
        )}
        {actions.map((a) => (
          <button
            key={a.label}
            className={`btn ${a.variant === 'danger' ? 'btn-danger' : ''} ${a.variant === 'primary' ? 'btn-primary' : ''}`}
            onClick={() => onAction(a)}
          >
            {a.label}
          </button>
        ))}
      </div>

      {/* Quick stats */}
      {stats && (
        <div className="ot-stats">
          <button className="ot-stat-card" onClick={() => onTabChange('code')}>
            <div className="ot-stat-num">{stats.code_changes}</div>
            <div className="ot-stat-label">代码变更</div>
          </button>
          <button className="ot-stat-card" onClick={() => onTabChange('test')}>
            <div className="ot-stat-num">{stats.tokens.calls}</div>
            <div className="ot-stat-label">LLM 调用</div>
          </button>
          <div className="ot-stat-card ot-stat-card-static">
            <div className="ot-stat-num">
              {stats.tokens.total_tokens >= 1000
                ? `${(stats.tokens.total_tokens / 1000).toFixed(1)}K`
                : stats.tokens.total_tokens}
            </div>
            <div className="ot-stat-label">Token · ¥{stats.tokens.cost_cny.toFixed(2)}</div>
          </div>
        </div>
      )}

      {/* Token breakdown */}
      {stats && stats.tokens.total_tokens > 0 && (
        <div className="ot-token-breakdown">
          <div className="ot-token-row">
            <span>Prompt</span>
            <span>{stats.tokens.prompt_tokens.toLocaleString()}</span>
          </div>
          <div className="ot-token-row">
            <span>Completion</span>
            <span>{stats.tokens.completion_tokens.toLocaleString()}</span>
          </div>
          {Object.entries(stats.tokens.by_stage).length > 0 && (
            <div className="ot-token-group">
              <div className="ot-token-group-title">按阶段</div>
              {Object.entries(stats.tokens.by_stage).map(([stage, tokens]) => (
                <div key={stage} className="ot-token-row">
                  <span>{stage}</span>
                  <span>{tokens.toLocaleString()}</span>
                </div>
              ))}
            </div>
          )}
          {Object.entries(stats.tokens.by_model).length > 0 && (
            <div className="ot-token-group">
              <div className="ot-token-group-title">按模型</div>
              {Object.entries(stats.tokens.by_model).map(([model, tokens]) => (
                <div key={model} className="ot-token-row">
                  <span>{model || '未知模型'}</span>
                  <span>{tokens.toLocaleString()}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Context content merged into overview */}
      <div className="ot-context-section">
        <ContextTab storyKey={storyKey} />
      </div>
    </div>
  )
}
