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

  async function handleConfirmDoc(docType: string) {
    const ok = await docApi.confirm(storyKey, docType).then(() => true).catch(() => false)
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

      {/* gate 推进卡片:当前状态可推进时显示「进入 X」按钮 */}
      {gate && (
        <div className={`ot-gate-card${gate.all_satisfied ? ' satisfied' : ''}`}>
          <div className="ot-gate-title">
            {gate.all_satisfied
              ? `✅ 成果物就绪,可进入 ${gate.to}`
              : `进入 ${gate.to} 需要以下成果物`}
          </div>
          {!gate.all_satisfied && (
            <div className="ot-gate-missing">
              {gate.required.filter((r) => !r.satisfied).map((r) => (
                <span key={r.key} className="ot-gate-missing-item">
                  {r.label}{r.exists && r.needs_confirm && !r.confirmed ? '(未确认)' : '(未完成)'}
                </span>
              ))}
            </div>
          )}
          {gate.all_satisfied && (
            <button className="btn btn-primary" onClick={onAdvanceLifecycle}>
              确认进入 {gate.to} →
            </button>
          )}
        </div>
      )}

      {/* 第二层:成果物清单(自动检测 + 人工确认 + 可跳过) */}
      {deliverables.length > 0 && (
        <div className="ot-deliverables">
          <h3 className="ot-deliv-title">📦 交付物</h3>
          <div className="ot-deliv-list">
            {deliverables.map((d) => (
              <div key={d.key} className={`ot-deliv-item${d.satisfied ? ' done' : ''}`}>
                <span className="ot-deliv-icon">{d.icon}</span>
                <span className="ot-deliv-label">{d.label}</span>
                <span className={`ot-deliv-status ${d.satisfied ? 'ok' : 'pending'}`}>
                  {d.skipped ? '⊘ 已跳过'
                    : d.confirmed ? '✓ 已确认'
                    : d.exists ? (d.needs_confirm ? '⚠ 待确认' : '✓ 已有')
                    : '✗ 未完成'}
                </span>
                {/* 操作按钮 */}
                {!d.satisfied && (
                  <div className="ot-deliv-actions">
                    {/* doc 类:存在但需确认 → 确认按钮 */}
                    {d.exists && d.needs_confirm && !d.confirmed && !d.skipped && (
                      <button className="btn btn-sm btn-primary" onClick={() => handleConfirmDoc(d.key === 'spec' ? 'spec' : d.key === 'test_report' ? 'test_report' : d.key)}>
                        ✓ 确认
                      </button>
                    )}
                    {/* 跳过 */}
                    {!d.skipped && (
                      <button className="btn btn-sm" onClick={() => handleSkipDeliverable(d.key)}>
                        跳过
                      </button>
                    )}
                  </div>
                )}
              </div>
            ))}
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
