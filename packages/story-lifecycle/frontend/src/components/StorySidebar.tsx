import { useState, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { deliverablesApi, docApi } from '../api/client'
import type { DeliverableItem, GateInfo } from '../api/client'
import './StorySidebar.css'

interface Module {
  id: string
  icon: string
  label: string
  badge?: number
  badgeVariant?: 'default' | 'danger'
}

interface Props {
  storyKey: string
  modules: Module[]
  activeModule: string
  onModuleChange: (id: string) => void
  /** 交付物精准跳转:跳 tab + 可选打开特定 doc(如 spec)。 */
  onNavigate: (tab: string, doc?: string) => void
  onBack?: () => void
  /** TAPD 需求页 URL;空时按 storyKey(tapd-*)推导,都拿不到则不显示入口。 */
  tapdUrl?: string
  /** gate 推进(进入下一 lifecycle 状态)入口;调 POST /lifecycle/advance。 */
  onAdvance?: () => void
}

// 模块导航图标(概览/代码/文档/测试场景 tab)—— 统一 1.5px 描边 SVG,不用 emoji。
const MODULE_ICONS: Record<string, ReactNode> = {
  overview: (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4">
      <rect x="1.75" y="1.75" width="5" height="5" rx="1.25" />
      <rect x="9.25" y="1.75" width="5" height="5" rx="1.25" />
      <rect x="1.75" y="9.25" width="5" height="5" rx="1.25" />
      <rect x="9.25" y="9.25" width="5" height="5" rx="1.25" />
    </svg>
  ),
  code: (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
      <path d="M5.75 4.75 2.5 8l3.25 3.25M10.25 4.75 13.5 8l-3.25 3.25" />
    </svg>
  ),
  docs: (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
      <path d="M4.25 1.75h4.5l3 3v9.5h-7.5z" />
      <path d="M8.75 1.75v3h3" />
    </svg>
  ),
  // 测试场景:烧瓶(flask)—— 测试验证语义
  scenarios: (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
      <path d="M6.25 1.75h3.5M7 1.75v3.4L4.25 10.6a2.6 2.6 0 0 0 2.2 3.9h3.1a2.6 2.6 0 0 0 2.2-3.9L9 5.15V1.75" />
      <path d="M4.4 9.5h7.2" />
    </svg>
  ),
}

const ICON_EXTERNAL = (
  <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
    <path d="M6.5 3.5h-3v9h9v-3" />
    <path d="M9.5 3.5h3v3" />
    <path d="M12.25 3.75 7.5 8.5" />
  </svg>
)

// TAPD 需求页 URL:优先用后端给的 tapdUrl;否则按 storyKey(tapd-<fullId>)推导
// (与 OverviewTab 头部同一规则:fullId 第 3~10 位是 workspace id)。
function resolveTapdUrl(storyKey: string, tapdUrl?: string): string {
  if (tapdUrl) return tapdUrl
  const fullId = storyKey.startsWith('tapd-') ? storyKey.slice(5) : ''
  const ws = fullId.length >= 10 ? fullId.slice(2, 10) : ''
  return ws ? `https://www.tapd.cn/${ws}/prong/stories/view/${fullId}` : ''
}

// 交付物 key → {跳转 tab, 可选打开的 doc_type}。
// doc 类(doc_type)→ docs tab + 打开该 doc;code → code tab。
// delivery 不在此表:它是 MR 驱动(查 story_delivery_artifact),不是文档,story_doc
// 永远没有 delivery 行 —— 若指到 /docs/delivery 必然 404 空文档。它的产物靠 d.evidence
// 内联展开显示(见 DELIVERY_EXPANDABLE),不跳 tab。
const DELIV_TARGET: Record<string, { tab: string; doc?: string }> = {
  prd: { tab: 'docs', doc: 'prd' },
  spec: { tab: 'docs', doc: 'spec' },
  code: { tab: 'code' },
  test_report: { tab: 'docs', doc: 'test_report' },
}

// delivery 交付物可内联展开 MR 产物清单(数据已随 /deliverables 回来,无需新请求)。
const DELIVERY_EXPANDABLE = 'delivery'

// doc 类交付物(spec/test_report/prd)走 docApi.confirm(写 story_doc);
// 非 doc 类(code/delivery)走 deliverablesApi.confirm(写 context_json)。
const DOC_DELIVERABLES = new Set(['spec', 'test_report', 'prd'])

// 交付物状态点:状态 → 说明文案(tooltip)。视觉见 .ss-dot 各 modifier。
const DOT_STATUS_TEXT: Record<string, string> = {
  done: '已确认',
  pending: '有产物,待确认',
  ready: '有产物',
  empty: '暂无产物',
  skipped: '已跳过',
}

/**
 * StorySidebar — 左侧导航。
 *
 * 结构:
 *   - 返回
 *   - 模块导航(概览/代码/文档 tab)
 *   - 交付物导航:每项一行 = 状态点 + 标签 + 确认圈/跳过(悬停显现)。
 *     状态点表达产物/确认状态(替代旧的「有产物」小字行);点击项 = 精准跳转。
 *   - gate 推进入口(进入下一状态):未满足时按钮+缺失原因合成锁定卡片
 *   - 底部操作(打开 TAPD;非 TAPD story 不显示)
 *
 * 确认模式选择(NN/g + Eleken 研究):用户主动确认完成 → checkbox 是正确模式
 * (deliberate commit);产物不存在(前置条件未满足)→ 确认圈置灰禁用。
 */
export default function StorySidebar({
  storyKey, modules, activeModule, onModuleChange, onNavigate, onBack, tapdUrl, onAdvance,
}: Props) {
  const qc = useQueryClient()

  const { data: delivData } = useQuery({
    queryKey: ['deliverables', storyKey],
    queryFn: () => deliverablesApi.get(storyKey),
    enabled: !!storyKey,
    refetchInterval: 15000,
  })

  const deliverables: DeliverableItem[] = delivData?.deliverables ?? []
  const gate: GateInfo | null = delivData?.gate ?? null

  // delivery 的 MR 产物清单展开态:有 evidence 才能展开,无产物点击无反应。
  const [deliveryExpanded, setDeliveryExpanded] = useState(false)

  async function handleConfirm(delivKey: string) {
    const ok = DOC_DELIVERABLES.has(delivKey)
      ? await docApi.confirm(storyKey, delivKey).then(() => true).catch(() => false)
      : await deliverablesApi.confirm(storyKey, delivKey).then(() => true).catch(() => false)
    if (ok) qc.invalidateQueries({ queryKey: ['deliverables', storyKey] })
  }

  async function handleSkip(delivKey: string) {
    const ok = await deliverablesApi.skip(storyKey, delivKey).then(() => true).catch(() => false)
    if (ok) qc.invalidateQueries({ queryKey: ['deliverables', storyKey] })
  }

  function handleDelivClick(d: DeliverableItem) {
    const target = DELIV_TARGET[d.key]
    if (target) {
      onNavigate(target.tab, target.doc)
      return
    }
    // delivery:内联展开 MR 产物(非跳转);只在有 evidence 时可展开。
    if (d.key === DELIVERY_EXPANDABLE && (d.evidence?.length ?? 0) > 0) {
      setDeliveryExpanded((v) => !v)
    }
  }

  // gate 未满足时,缺失的成果物名(下方显示)。
  const gateMissing = gate && !gate.all_satisfied
    ? gate.required.filter((r) => !r.satisfied).map((r) => r.label)
    : []

  return (
    <aside className="story-sidebar">
      {onBack && (
        <button className="ss-back" onClick={onBack}>
          <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M10 3.5 5.5 8l4.5 4.5" />
          </svg>
          返回
        </button>
      )}

      {/* 模块导航(概览/代码/文档) */}
      <nav className="ss-nav">
        {modules.map((m) => (
          <button
            key={m.id}
            className={`ss-nav-item ${activeModule === m.id ? 'active' : ''}`}
            onClick={() => onModuleChange(m.id)}
          >
            <span className="ss-icon">{MODULE_ICONS[m.id] ?? m.icon}</span>
            <span className="ss-label">{m.label}</span>
            {m.badge != null && (
              <span className={`ss-badge ${m.badgeVariant === 'danger' ? 'ss-badge-danger' : ''}`}>
                {m.badge}
              </span>
            )}
          </button>
        ))}
      </nav>

      {/* 交付物导航:状态点 + 确认圈 + 悬停跳过 + 点击精准跳转 */}
      {deliverables.length > 0 && (
        <div className="ss-deliverables">
          <div className="ss-deliv-head">
            <span className="ss-deliv-title">交付物</span>
          </div>
          <div className="ss-deliv-list">
            {deliverables.map((d) => {
              const skipped = !!d.skipped
              const showConfirm = !!d.needs_confirm && !skipped
              const target = DELIV_TARGET[d.key]
              const hasEvidence = d.key === DELIVERY_EXPANDABLE && (d.evidence?.length ?? 0) > 0
              // 有 target 的(doc/code)→ 跳 tab;delivery 有 MR 产物 → 内联展开;否则不可点。
              const clickable = (!skipped) && (!!target || hasEvidence)
              // 确认圈可用性:产物存在才能确认;无产物 → 置灰禁用。
              const confirmDisabled = !d.exists
              const status = skipped
                ? 'skipped'
                : d.satisfied
                  ? 'done'
                  : d.exists
                    ? showConfirm ? 'pending' : 'ready'
                    : 'empty'
              const clickTitle = target
                ? `查看${d.label}`
                : hasEvidence
                  ? `${d.label}:展开合并记录`
                  : `${d.label}(无对应 tab)`
              return (
                <div
                  key={d.key}
                  className={`ss-deliv-item ss-status-${status}`}
                >
                  <div
                    className={`ss-deliv-main${clickable ? ' clickable' : ''}`}
                    title={clickTitle}
                    onClick={() => clickable && handleDelivClick(d)}
                  >
                    <span className="ss-dot" title={DOT_STATUS_TEXT[status]} />
                    <span className="ss-deliv-label">{d.label}</span>
                    {skipped && <span className="ss-deliv-skipped-tag">已跳过</span>}
                    {/* 确认圈:可点 = 确认动作;无产物置灰禁用 */}
                    {showConfirm && (
                      <input
                        type="checkbox"
                        className="ss-check"
                        checked={!!d.confirmed}
                        disabled={confirmDisabled}
                        title={confirmDisabled ? '产物未生成,无法确认' : (d.confirmed ? '已确认' : '点击确认')}
                        onClick={(e) => e.stopPropagation()}
                        onChange={() => !confirmDisabled && handleConfirm(d.key)}
                      />
                    )}
                    {/* 跳过按钮(悬停显现) */}
                    {!skipped && (
                      <button
                        className="ss-deliv-skip"
                        title="跳过此交付物"
                        onClick={(e) => { e.stopPropagation(); handleSkip(d.key) }}
                      >
                        <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round">
                          <circle cx="8" cy="8" r="5.75" />
                          <path d="M4.1 4.1 11.9 11.9" />
                        </svg>
                      </button>
                    )}
                  </div>
                  {/* delivery 的 MR 产物清单(展开态)。delivery 是 MR 驱动,产物就是这些
                      合并记录 —— 不是文档,不进 docs tab,直接内联展示。 */}
                  {d.key === DELIVERY_EXPANDABLE && deliveryExpanded && hasEvidence && (
                    <div className="ss-deliv-evidence">
                      {d.evidence!.map((mr, i) => (
                        <div className="ss-mr-card" key={i}>
                          <div className="ss-mr-head">
                            <span className={`ss-mr-state ss-mr-state-${mr.delivery_state ?? 'unknown'}`}>
                              {mr.delivery_state === 'merged' ? '已合并'
                                : mr.delivery_state === 'abandoned' ? '已废弃'
                                : mr.delivery_state ?? '未知'}
                            </span>
                            {mr.external_id && <span className="ss-mr-id">#{mr.external_id}</span>}
                            {mr.url && (
                              <a className="ss-mr-link" href={mr.url} target="_blank" rel="noreferrer">
                                打开 ↗
                              </a>
                            )}
                          </div>
                          {(mr.source_branch || mr.target_branch) && (
                            <div className="ss-mr-branches">
                              <code>{mr.source_branch || '?'}</code>
                              <span className="ss-mr-arrow">→</span>
                              <code>{mr.target_branch || '?'}</code>
                            </div>
                          )}
                          {mr.evidence_ref && <div className="ss-mr-note">{mr.evidence_ref}</div>}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )
            })}
          </div>

          {/* gate 推进入口:all_satisfied 可点;未满足时按钮+原因合成一个锁定卡片,
              一眼看出「缺什么 → 所以进不去」。 */}
          {gate && (
            <div className={`ss-gate-wrap ${gate.all_satisfied ? 'ready' : 'locked'}`}>
              <button
                className={`ss-gate-btn ${gate.all_satisfied ? 'ready' : 'locked'}`}
                disabled={!gate.all_satisfied}
                title={gate.all_satisfied ? `进入 ${gate.to}` : `还差: ${gateMissing.join('、')}`}
                onClick={onAdvance}
              >
                进入 {gate.to}
                <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M3 8h10M9.5 4.5 13 8l-3.5 3.5" />
                </svg>
              </button>
              {!gate.all_satisfied && gateMissing.length > 0 && (
                <div className="ss-gate-reason">
                  <span className="ss-gate-reason-dot" />
                  还差 {gateMissing.join('、')},完成后可进入
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {resolveTapdUrl(storyKey, tapdUrl) && (
        <div className="ss-bottom-actions">
          <a
            className="ss-tapd-btn"
            href={resolveTapdUrl(storyKey, tapdUrl)}
            target="_blank"
            rel="noreferrer"
          >
            {ICON_EXTERNAL}
            打开 TAPD
          </a>
        </div>
      )}
    </aside>
  )
}
