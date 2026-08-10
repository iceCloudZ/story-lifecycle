import type { Story, AgentAction, ActionButton, Plan } from '../api/client'
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
  // adapter 切换:下沉到终端区后按 stage 索引(不再是数组 index)。
  onActionAdapterChange: (stage: string, adapter: string) => void
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
  // doneStages 现在不再在 OverviewTab 算(TerminalTab 的 stage 卡片自己读 planData.stages)。

  return (
    <div className="tab-content overview-tab">
      {/* 头部卡片:上行 = 标题/元信息 + 操作;下行 = 全宽 lifecycle 步骤条。
          交付物 + gate 推进入口已移到左侧 sidebar(导航=交付物)。
          lastError(如「No actions to execute」)作为卡内告警条贴底部 ——
          业务状态条本就表达 story 进度,错误信息贴这里语义最顺。 */}
      <div className="ui-card ot-header">
        <div className="ot-header-top">
          <div className="ot-lc-meta">
            <span className="ot-title">{detail.title || detail.storyKey}</span>
            <span className="ot-submeta">
              {[
                detail.storyKey,
                profileLabel[detail.profile] || detail.profile,
                `${detail.currentStage} 重试 ${detail.executionCount}/3`,
                detail.priority,
                detail.sourceType,
              ]
                .filter(Boolean)
                .join(' · ')}
            </span>
          </div>
          <div className="ot-lc-actions">
            {onResolve && (
              <button className="btn btn-sm btn-primary" onClick={onResolve}>标记已修复</button>
            )}
            {(() => {
              const fullId = detail.storyKey.startsWith('tapd-') ? detail.storyKey.slice(5) : ''
              const ws = fullId.length >= 10 ? fullId.slice(2, 10) : ''
              const url = detail.tapdUrl || (ws ? `https://www.tapd.cn/${ws}/prong/stories/view/${fullId}` : '')
              return url ? <a className="ot-tapd-link" href={url} target="_blank" rel="noreferrer">TAPD ↗</a> : null
            })()}
            <span className="ot-updated">{detail.updatedAt}</span>
          </div>
        </div>
        <div className="ot-lc-nodes">
          {LIFECYCLE_ORDER.map((state, i) => {
            const isDone = i < curIdx
            const isCurrent = i === curIdx
            return (
              <div key={state} className={`ot-lc-item${isCurrent ? ' current' : ''}${isDone ? ' done' : ''}`}>
                <span className="ot-lc-node">
                  {isDone ? (
                    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M3.5 8.5 6.5 11.5 12.5 4.5" />
                    </svg>
                  ) : (
                    <span className="ot-lc-num">{i + 1}</span>
                  )}
                </span>
                <span className="ot-lc-label">{state}</span>
                {i < LIFECYCLE_ORDER.length - 1 && <span className="ot-lc-line" />}
              </div>
            )
          })}
        </div>
        {detail.lastError && (
          <div className="ui-alert ui-alert-danger ot-header-error" title={detail.lastError}>
            <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
              <path d="M8 5.5v3.5" />
              <circle cx="8" cy="11.25" r="0.25" fill="currentColor" />
              <path d="M7 2.75 1.9 12.25a1 1 0 0 0 .87 1.5h10.46a1 1 0 0 0 .87-1.5L9 2.75a1.16 1.16 0 0 0-2 0z" />
            </svg>
            {detail.lastError}
          </div>
        )}
      </div>

      {/* Agent 规划已下沉到终端区(跟 session/历史合并成 stage 卡片)。
          操作按钮(继续/重试/紧急停止)留这里。 */}

      {/* 操作按钮 */}
      {((detail.status === 'active' && neverStarted && !!detail.hasPlan) || rowActions.length > 0) && (
        <div className="ot-actions">
          {detail.status === 'active' && neverStarted && !!detail.hasPlan && (
            <button className="btn btn-primary" onClick={onStart}>开始执行</button>
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

      {/* 终端区:有 plan → stage 卡片视图(action + session + 历史);
          无 plan → 现有 session tab 视图。规划确认按钮在 stage 卡片栏右上。 */}
      <div id="overview-terminal" className="ui-card ot-terminal-section">
        <h3 className="ui-section-title ot-terminal-title">终端</h3>
        <TerminalTab
          storyKey={storyKey}
          status={detail.status}
          actions={resolvedActions}
          stages={planData?.stages}
          currentStage={detail.currentStage}
          isConfirmed={isConfirmed}
          editable={!!detail.hasPlan && !isConfirmed}
          onAdapterChange={onActionAdapterChange}
          onConfirmPlan={onConfirmPlan}
          onRegeneratePlan={onRegeneratePlan}
        />
      </div>

      {/* 半自动工具(置底:日常全自动跑不用,手动介入时才翻) */}
      <SemiAutoSection storyKey={storyKey} />
    </div>
  )
}
