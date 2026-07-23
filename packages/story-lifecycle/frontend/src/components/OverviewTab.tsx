import { storyApi } from '../api/client'
import type { Story, AgentAction, ActionButton, Plan, PlanStage, StoryStateView } from '../api/client'
import StageProgress from './StageProgress'
import ActionCard from './ActionCard'
import SemiAutoSection from './SemiAutoSection'
import TerminalTab from './TerminalTab'

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
  onAdvanceLifecycle: () => void
  onActionAdapterChange: (index: number, adapter: string) => void
  // single-pass 等 profile 创建即 active 但从未启动(无 _active_execution):
  // overview 显示「开始执行」按钮首次启动它。
  neverStarted: boolean
  onStart: () => void
  // bug 类 story 的「标记已修复」入口(原顶部 topbar,迁入头部右侧)。
  onResolve?: () => void
}

export default function OverviewTab({
  storyKey, detail, resolvedActions, isConfirmed, planData,
  onConfirmPlan, onRegeneratePlan, onAction, actions, onAdvanceLifecycle,
  onActionAdapterChange, neverStarted, onStart, onResolve,
}: Props) {
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

  // STORY-STATE-MODEL: Story 业务状态(主进度条)+ 状态闸(优先于阶段闸)。
  // story_states 从 /plan 读;无 → 不显示主状态条(向后兼容无 story_states 的 profile)。
  const storyStates: StoryStateView[] = planData?.story_states ?? []
  const storyStateGate = planData?.story_state_gate ?? null
  const showStateGateCard =
    detail.status === 'paused' && !!storyStateGate?.awaiting_confirm

  // paused「继续执行」/ blocked「重试」是主恢复路径,提到进度条下方做醒目主按钮;
  // 底部按钮行不再重复它(其余 status 的按钮行行为不变)。
  const primaryAction = actions.find((a) => a.variant === 'primary') ?? null
  const rowActions = primaryAction ? actions.filter((a) => a !== primaryAction) : actions

  // 阶段完成标记(planData.stages[].done)→ ActionCard 隐藏已完成阶段的「执行」。
  const doneStages = new Set(
    (planData?.stages ?? []).filter((s) => s.done).map((s) => s.name)
  )

  // profile 名 → 可读标签(头部元信息行用)
  const profileLabel: Record<string, string> = {
    minimal: '最小开发流程',
    realtest: '真机测试流程',
    strict: '严格流程',
    swebench: 'SWE-bench 评测',
    'headless-smoke': 'headless 冒烟',
    demo: '演示流程',
  }

  return (
    <div className="tab-content overview-tab">
      {/* Top bar — 标题 + key + 元信息(原信息卡片行,并入头部)/ TAPD 跳转 + 更新时间 */}
      <div className="ot-header">
        <div className="ot-header-left">
          <span className="ot-title">{detail.title || detail.storyKey}</span>
          <span className="ot-key">{detail.storyKey}</span>
          <span className="ot-meta">
            {[
              profileLabel[detail.profile] || detail.profile,
              `${detail.currentStage} 重试 ${detail.executionCount}/3`,
              detail.priority,
              detail.sourceType,
            ]
              .filter(Boolean)
              .join(' · ')}
          </span>
        </div>
        <div className="ot-header-right">
          {detail.lastError && (
            <span className="ot-error-badge" title={detail.lastError}>⚠ {detail.lastError}</span>
          )}
          {onResolve && (
            <button className="btn btn-sm btn-primary" onClick={onResolve}>标记已修复</button>
          )}
          {(() => {
            // TAPD 跳转:优先 tapdUrl(后端同步时填);否则从 tapd- 前缀的 key 推导。
            // story_id 格式:11{workspace 8位}{流水号} → URL /{ws}/prong/stories/view/{full_id}
            const fullId = detail.storyKey.startsWith('tapd-')
              ? detail.storyKey.slice(5)
              : ''
            const ws = fullId.length >= 10 ? fullId.slice(2, 10) : ''
            const url =
              detail.tapdUrl ||
              (ws ? `https://www.tapd.cn/${ws}/prong/stories/view/${fullId}` : '')
            return url ? (
              <a className="ot-tapd-link" href={url} target="_blank" rel="noreferrer">
                TAPD ↗
              </a>
            ) : null
          })()}
          <span className="ot-updated">更新: {detail.updatedAt}</span>
        </div>
      </div>

      {/* 合并进度条:Story 业务状态为主节点(开发/测试/上线),每个状态展开它的阶段。
          替掉原来两个重复的进度条(Story 状态条 + StageProgress)。无 story_states 时
          退化用 StageProgress(向后兼容无 story_states 的 profile)。 */}
      {storyStates.length > 0 ? (
        <div className="ot-story-state-progress">
          {storyStates.map((st) => {
            const cls = st.done
              ? 'ot-ss-node done'
              : st.current
                ? 'ot-ss-node current'
                : 'ot-ss-node'
            return (
              <div key={st.name} className="ot-ss-item">
                <div className="ot-ss-head">
                  <span className={cls}>
                    {st.done ? '✓' : st.current ? '●' : '○'}
                  </span>
                  <span className={`ot-ss-label ${st.current ? 'active' : ''}`}>
                    {st.name}
                  </span>
                </div>
                {/* 展开该状态下的阶段(design✓ / build● / ...) */}
                {st.stages.length > 0 && (
                  <div className="ot-ss-stages">
                    {st.stages.map((sg) => {
                      const isDone = (planData?.stages ?? []).some(
                        (p) => p.name === sg && p.done
                      )
                      const isRunning = sg === detail.currentStage && st.current && !isDone
                      const scls = isDone
                        ? 'sg-chip done'
                        : isRunning
                          ? 'sg-chip running'
                          : 'sg-chip'
                      return (
                        <span key={sg} className={scls}>
                          {isDone ? '✓ ' : isRunning ? '● ' : ''}
                          {sg}
                        </span>
                      )
                    })}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      ) : (
        <StageProgress stages={stages} currentStage={detail.currentStage} />
      )}

      {/* 主恢复路径(paused 继续执行 / blocked 重试)提到进度条下方,不再埋在按钮行里 */}
      {primaryAction && (
        <div className="ot-continue-banner">
          <button className="btn btn-primary" onClick={() => onAction(primaryAction)}>
            ▶ {primaryAction.label}
          </button>
        </div>
      )}

      {/* STORY-STATE-MODEL: Story 状态闸卡片(业务层,优先于阶段间闸) */}
      {showStateGateCard && (
        <div className="ot-story-state-gate-card">
          <div className="ot-story-state-gate-title">
            ✅ {storyStateGate?.from} 阶段全部完成
          </div>
          <div className="ot-story-state-gate-hint">
            {storyStateGate?.label || `确认进入 ${storyStateGate?.to}`}
          </div>
          <button
            className="btn btn-primary"
            onClick={onAdvanceLifecycle}
          >
            {storyStateGate?.label || `进入 ${storyStateGate?.to}`} →
          </button>
        </div>
      )}

      {/* 确认闸卡片(stage gate):仅当 Story 状态闸未显示时才显示(不抢主位) */}
      {showGateCard && !showStateGateCard && (
        <div className="ot-stage-gate-card">
          <div className="ot-stage-gate-title">
            ✅ {stageGate?.completed_stage} 已完成
          </div>
          <div className="ot-stage-gate-hint">
            确认推进到 <strong>{stageGate?.next_stage}</strong>?
          </div>
          <button
            className="btn btn-primary"
            onClick={async () => {
              // BUG #20: stage gate advance(PUT /advance = driver resume)成功后也跳终端,
              // 与 lifecycle advance / confirm plan 统一(所有"启动执行"按钮都跳终端)。
              const ok = await storyApi.advance(storyKey)
              if (ok) {
                // 终端在本页底部,滚动过去看输出(不再切 tab)。
                document.getElementById('overview-terminal')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
              }
            }}
          >
            确认推进 → {stageGate?.next_stage}
          </button>
        </div>
      )}

      {/* Agent planning area — 规划期可改 adapter;执行期作为阶段执行入口
          (执行=全自动 spawn 终端,复制提示词=半自动手动跑)。
          确认/重新规划按钮收进卡片头部,操作对象和按钮同处一屏。 */}
      {resolvedActions.length > 0 && (
        <div className="ot-plan-section">
          <div className="ot-plan-head">
            <div>
              <h3>🤖 Agent 规划</h3>
              <p className="ot-plan-hint">
                复制提示词后可贴到自己的 CLI 执行，完成后系统会自动认领结果
              </p>
            </div>
            {detail.status === 'planning' && !isConfirmed && (
              <div className="ot-plan-actions">
                <button className="btn" onClick={onRegeneratePlan}>
                  🔄 重新规划
                </button>
                <button className="btn btn-primary" onClick={onConfirmPlan}>
                  ✅ 确认规划，开始执行
                </button>
              </div>
            )}
          </div>
          <div className="action-cards">
            {resolvedActions.map((a, i) => (
              <ActionCard
                key={i}
                action={a}
                index={i}
                storyKey={storyKey}
                done={!!a.stage && doneStages.has(a.stage)}
                editable={detail.status === 'planning' && !isConfirmed}
                onAdapterChange={onActionAdapterChange}
              />
            ))}
          </div>
        </div>
      )}

      {/* Action buttons(确认/重新规划已收进 Agent 规划卡片头部;这里只剩
          single-pass 启动和各状态的操作按钮) */}
      <div className="ot-actions">
        {/*
          single-pass 等 profile 创建即 active,但执行从未触发(无 _active_execution)。
          planning 走的是「确认规划」按钮;这种 active-unstarted story 走「开始执行」
          直接 start_story_async(跳过 planning 确认闸,PRD 已有)。已在跑的不显示。
        */}
        {detail.status === 'active' && neverStarted && (
          <button className="btn btn-primary" onClick={onStart}>
            🚀 开始执行
          </button>
        )}
        {rowActions.map((a) => (
          <button
            key={a.label}
            className={`btn ${a.variant === 'danger' ? 'btn-danger' : ''} ${a.variant === 'primary' ? 'btn-primary' : ''}`}
            onClick={() => onAction(a)}
          >
            {a.label}
          </button>
        ))}
      </div>

      {/* 半自动工具(原 ContextTab 收敛):复制资料包/上线提示词/PRD/工作区 */}
      <SemiAutoSection storyKey={storyKey} />

      {/* 终端区(原独立 tab 并入):执行类操作成功后滚动到此看 CLI 输出。
          有会话显示终端面板,无则空态。 */}
      <div id="overview-terminal" className="ot-terminal-section">
        <h3 className="ot-terminal-title">💻 终端</h3>
        <TerminalTab storyKey={storyKey} status={detail.status} />
      </div>
    </div>
  )
}
