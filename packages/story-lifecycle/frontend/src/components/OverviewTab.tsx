import { useQuery, useQueryClient } from '@tanstack/react-query'
import { deliverablesApi, docApi } from '../api/client'
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
  onAdvanceLifecycle: () => void
  onActionAdapterChange: (index: number, adapter: string) => void
  neverStarted: boolean
  onStart: () => void
  onResolve?: () => void
}

export default function OverviewTab({
  storyKey, detail, resolvedActions, isConfirmed, planData,
  onConfirmPlan, onRegeneratePlan, onAction, actions, onAdvanceLifecycle,
  onActionAdapterChange, neverStarted, onStart, onResolve,
}: Props) {
  const qc = useQueryClient()

  // 成果物清单 + gate 状态(第二层进度条)。
  const { data: delivData } = useQuery({
    queryKey: ['deliverables', storyKey],
    queryFn: () => deliverablesApi.get(storyKey),
    enabled: !!storyKey,
    refetchInterval: 15000,
  })

  const deliverables = delivData?.deliverables ?? []
  const gate = delivData?.gate ?? null

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

  // doc 类(spec/test_report)走 docApi.confirm(写 story_doc);
  // 非 doc 类(code/delivery)走 deliverablesApi.confirm(写 context_json)。
  const DOC_DELIVERABLES = new Set(['spec', 'test_report'])
  async function handleConfirm(delivKey: string) {
    const ok = DOC_DELIVERABLES.has(delivKey)
      ? await docApi.confirm(storyKey, delivKey).then(() => true).catch(() => false)
      : await deliverablesApi.confirm(storyKey, delivKey).then(() => true).catch(() => false)
    if (ok) qc.invalidateQueries({ queryKey: ['deliverables', storyKey] })
  }

  async function handleSkipDeliverable(delivKey: string) {
    const ok = await deliverablesApi.skip(storyKey, delivKey).then(() => true).catch(() => false)
    if (ok) qc.invalidateQueries({ queryKey: ['deliverables', storyKey] })
  }

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
          {detail.lastError && (
            <span className="ot-error-badge" title={detail.lastError}>⚠ {detail.lastError}</span>
          )}
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

      {/* 第一层:业务状态条(纯状态节点,成果物 gate 驱动推进) */}
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

      {/* 第二层:成果物清单(自动检测 + 人工确认 + 可跳过)
          横排卡片,每卡两个灯:产物灯(只读) + 确认灯(可点击=确认动作)。
          灯含义由标题右侧图例说明一次,卡片里不重复标注。
          gate 推进入口并入此区标题右侧:all_satisfied 时按钮可点(进入下一状态),
          未满足时按钮置灰 + tooltip 列出缺的成果物 —— 不再单开一张 gate 卡片。 */}
      {deliverables.length > 0 && (
        <div className="ot-deliverables">
          <div className="ot-deliv-head">
            <h3 className="ot-deliv-title">📦 交付物</h3>
            <div className="ot-deliv-head-right">
              <span className="ot-deliv-legend" title="左:产物是否存在 · 右:是否已确认">
                <span className="ot-deliv-lamp off" title="产物" />
                <span className="ot-deliv-lamp off" title="确认" />
              </span>
              {gate && (
                <button
                  className={`btn btn-sm ${gate.all_satisfied ? 'btn-primary' : 'ot-gate-btn-disabled'}`}
                  disabled={!gate.all_satisfied}
                  title={
                    gate.all_satisfied
                      ? `进入 ${gate.to}`
                      : `还差: ${gate.required
                          .filter((r) => !r.satisfied)
                          .map((r) => r.label)
                          .join('、')}`
                  }
                  onClick={onAdvanceLifecycle}
                >
                  进入 {gate.to} →
                </button>
              )}
            </div>
          </div>
          <div className="ot-deliv-list">
            {deliverables.map((d) => {
              const skipped = !!d.skipped
              const showConfirm = !!d.needs_confirm && !skipped
              return (
                <div key={d.key} className={`ot-deliv-item${d.satisfied ? ' done' : ''}${skipped ? ' skipped' : ''}`}>
                  {!skipped && (
                    <button
                      className="ot-deliv-skip-btn"
                      title="跳过此成果物"
                      onClick={() => handleSkipDeliverable(d.key)}
                    >
                      ⊘
                    </button>
                  )}
                  <span className="ot-deliv-icon">{d.icon}</span>
                  <span className="ot-deliv-label">{d.label}</span>
                  {skipped ? (
                    <span className="ot-deliv-skipped-tag">⊘ 已跳过</span>
                  ) : (
                    <span className="ot-deliv-lamps">
                      {/* 产物灯:只读,exists → 绿实心 / 灰空心 */}
                      <span
                        className={`ot-deliv-lamp ${d.exists ? 'on' : 'off'}`}
                        title={`产物${d.exists ? '已存在' : '未生成'}`}
                      />
                      {/* 确认灯:可点击 → handleConfirm(doc 类写 story_doc,非 doc 类写 context_json)。
                          needs_confirm=false(如 PRD)的卡片不显示此灯(无需确认)。 */}
                      {showConfirm && (
                        <button
                          className={`ot-deliv-lamp clickable ${d.confirmed ? 'on' : 'off'}`}
                          title={d.confirmed ? '已确认' : '点击确认'}
                          onClick={() => handleConfirm(d.key)}
                        />
                      )}
                    </span>
                  )}
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* 主恢复按钮(paused 继续 / failed 重试) */}
      {primaryAction && (
        <div className="ot-continue-banner">
          <button className="btn btn-primary" onClick={() => onAction(primaryAction)}>
            ▶ {primaryAction.label}
          </button>
        </div>
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

      {/* 半自动工具 */}
      <SemiAutoSection storyKey={storyKey} />

      {/* 第三层:终端区(按 profile stage 分 tab) */}
      <div id="overview-terminal" className="ot-terminal-section">
        <h3 className="ot-terminal-title">💻 终端</h3>
        <TerminalTab storyKey={storyKey} status={detail.status} />
      </div>
    </div>
  )
}
