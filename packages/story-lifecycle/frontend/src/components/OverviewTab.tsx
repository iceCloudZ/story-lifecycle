import type { Story, AgentAction, ActionButton, Plan } from '../api/client'
import ActionCard from './ActionCard'
import SemiAutoSection from './SemiAutoSection'
import TerminalTab from './TerminalTab'

const LIFECYCLE_ORDER = ['待启动', '开发', '测试', '上线', '结项']

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
  onActionAdapterChange: (index: number, adapter: string) => void
  neverStarted: boolean
  onStart: () => void
  onResolve?: () => void
}

export default function OverviewTab({
  storyKey, detail, resolvedActions, isConfirmed, planData,
  onConfirmPlan, onRegeneratePlan, onAction, actions,
  onActionAdapterChange, neverStarted, onStart, onResolve,
}: Props) {
  const profileLabel: Record<string, string> = {
    minimal: '最小开发流程',
    realtest: '真机测试流程',
    strict: '严格流程',
    swebench: 'SWE-bench 评测',
    'headless-smoke': 'headless 冒烟',
    'single-pass': '单阶段全干',
    demo: '演示流程',
  }

  // 业务状态条(第一层):纯状态节点,不带 stage chip。
  const curLifecycle = detail.lifecycleState || '待启动'
  const curIdx = LIFECYCLE_ORDER.indexOf(curLifecycle)

  const primaryAction = actions.find((a) => a.variant === 'primary') ?? null
  const rowActions = primaryAction ? actions.filter((a) => a !== primaryAction) : actions
  const doneStages = new Set(
    (planData?.stages ?? []).filter((s) => s.done).map((s) => s.name)
  )

  return (
    <div className="tab-content overview-tab">
      {/* 头部:标题 + key + 元信息 */}
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
          {onResolve && (
            <button className="btn btn-sm btn-primary" onClick={onResolve}>标记已修复</button>
          )}
          {(() => {
            const fullId = detail.storyKey.startsWith('tapd-') ? detail.storyKey.slice(5) : ''
            const ws = fullId.length >= 10 ? fullId.slice(2, 10) : ''
            const url = detail.tapdUrl || (ws ? `https://www.tapd.cn/${ws}/prong/stories/view/${fullId}` : '')
            return url ? <a className="ot-tapd-link" href={url} target="_blank" rel="noreferrer">TAPD ↗</a> : null
          })()}
          <span className="ot-updated">更新: {detail.updatedAt}</span>
        </div>
      </div>

      {/* 第一层:业务状态条(纯状态节点,成果物 gate 驱动推进)。
          交付物 + gate 推进入口已移到左侧 sidebar(导航=交付物),这里只留状态条。
          lastError(如「No actions to execute」)作为状态条的标注贴在下方 ——
          业务状态条本就表达 story 进度,错误信息贴这里语义最顺。 */}
      <div className="ot-lifecycle-bar">
        {LIFECYCLE_ORDER.map((state, i) => {
          const isDone = i < curIdx
          const isCurrent = i === curIdx
          return (
            <div key={state} className={`ot-lc-item${isCurrent ? ' current' : ''}${isDone ? ' done' : ''}`}>
              <span className="ot-lc-node">
                {isDone ? '✓' : isCurrent ? '●' : '○'}
              </span>
              <span className="ot-lc-label">{state}</span>
              {i < LIFECYCLE_ORDER.length - 1 && <span className="ot-lc-line" />}
            </div>
          )
        })}
      </div>
      {detail.lastError && (
        <div className="ot-lifecycle-error" title={detail.lastError}>⚠ {detail.lastError}</div>
      )}

      {/* Agent 规划区 */}
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
                <button className="btn" onClick={onRegeneratePlan}>🔄 重新规划</button>
                <button className="btn btn-primary" onClick={onConfirmPlan}>✅ 确认规划，开始执行</button>
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

      {/* 操作按钮 */}
      {((detail.status === 'active' && neverStarted) || rowActions.length > 0) && (
        <div className="ot-actions">
          {detail.status === 'active' && neverStarted && (
            <button className="btn btn-primary" onClick={onStart}>🚀 开始执行</button>
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
      )}

      {/* 第三层:终端区(按 profile stage 分 tab) */}
      <div id="overview-terminal" className="ot-terminal-section">
        <h3 className="ot-terminal-title">💻 终端</h3>
        <TerminalTab storyKey={storyKey} status={detail.status} />
      </div>

      {/* 半自动工具(置底:日常全自动跑不用,手动介入时才翻) */}
      <SemiAutoSection storyKey={storyKey} />
    </div>
  )
}
