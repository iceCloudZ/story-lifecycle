import { useQuery } from '@tanstack/react-query'
import { statsApi, storyApi } from '../api/client'
import type { Story, AgentAction, ActionButton, Plan, PlanStage } from '../api/client'
import StageProgress from './StageProgress'
import ActionCard from './ActionCard'
import ContextTab from './ContextTab'

interface Props {
  storyKey: string
  detail: Story
  resolvedActions: AgentAction[]
  isConfirmed: boolean
  planData?: Plan
  onConfirmPlan: () => void
  onRegeneratePlan: () => void
  onAction: (action: ActionButton) => void
  actions: ActionButton[]
  onTabChange: (tabId: string) => void
}

export default function OverviewTab({
  storyKey, detail, resolvedActions, isConfirmed, planData,
  onConfirmPlan, onRegeneratePlan, onAction, actions, onTabChange,
}: Props) {
  const { data: stats } = useQuery({
    queryKey: ['stats', storyKey],
    queryFn: () => statsApi.get(storyKey),
    enabled: !!detail,
  })

  // stage 进度条用真实数据(PLAN-stage-confirm-gate):优先 /plan 回的 stages(done 标记
  // 驱动状态);无 plan 数据(legacy / 规划前)回落到 minimal 默认三阶段。StageProgress
  // 自己按 currentStage 把当前阶段标 running。
  const stages = (() => {
    const fromPlan = planData?.stages ?? []
    if (fromPlan.length > 0) {
      return fromPlan.map((s: PlanStage) => ({
        name: s.name,
        status: (s.done ? 'completed' : 'pending') as 'completed' | 'pending',
      }))
    }
    return [
      { name: 'design', status: 'pending' as const },
      { name: 'build', status: 'pending' as const },
      { name: 'verify', status: 'pending' as const },
    ]
  })()

  // 确认闸卡片(stage_gate):story paused 且后端写了 _stage_gate 时显示醒目引导,
  // 点「确认推进」走 /advance(替 paused 状态里不那么显眼的「继续执行」)。
  const stageGate = planData?.stage_gate ?? null
  const showGateCard =
    detail.status === 'paused' && !!stageGate?.awaiting_confirm

  return (
    <div className="tab-content overview-tab">
      {/* Top bar */}
      <div className="ot-header">
        <span className="ot-key">{detail.storyKey}</span>
        <span className="ot-updated">更新: {detail.updatedAt}</span>
      </div>

      {/* Progress bar */}
      <StageProgress stages={stages} currentStage={detail.currentStage} />

      {/* 确认闸卡片(stage gate):醒目引导人确认推进下一 stage */}
      {showGateCard && (
        <div className="ot-stage-gate-card">
          <div className="ot-stage-gate-title">
            ✅ {stageGate?.completed_stage} 已完成
          </div>
          <div className="ot-stage-gate-hint">
            确认推进到 <strong>{stageGate?.next_stage}</strong>?
          </div>
          <button
            className="btn btn-primary"
            onClick={() => storyApi.advance(storyKey)}
          >
            确认推进 → {stageGate?.next_stage}
          </button>
        </div>
      )}

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
        {/*
          主按钮统一走自动链路(PLAN-stage-confirm-gate):去掉孤立的「启动终端(HITL)」
          主按钮(它调 /sessions/spawn 旁路自动链路,跑完 design 无人衔接下一阶段)。
          planning → 「开始 design」走 /plan/confirm → continue_orchestrator_agent,
          由自动链路 spawn design 终端(前端 TerminalTab 能发现)。执行期终端入口仍由
          TerminalTab sidebar 提供(次要 debug 入口)。
        */}
        {detail.status === 'planning' && !isConfirmed && resolvedActions.length > 0 && (
          <>
            <button className="btn btn-primary" onClick={onConfirmPlan}>
              ✅ 开始 design ({resolvedActions.filter((a) => a.action === 'launch').length} 步)
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
