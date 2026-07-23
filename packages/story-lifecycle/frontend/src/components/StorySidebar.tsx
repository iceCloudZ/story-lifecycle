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
  onArchive?: () => void
  onBack?: () => void
  /** PRD 文件路径;有值时 sidebar 底部显示「打开 PRD」(任何 tab 都能开)。 */
  prdPath?: string
  /** gate 推进(进入下一 lifecycle 状态)入口;调 POST /lifecycle/advance。 */
  onAdvance?: () => void
}

// 模块导航图标(概览/代码/文档 tab)。
const MODULE_ICONS: Record<string, string> = {
  overview: '📊',
  code: '📦',
  docs: '📄',
}

// 交付物 key → {跳转 tab, 可选打开的 doc_type}。
// doc 类(doc_type)→ docs tab + 打开该 doc;code → code tab;delivery → 无目标(只展示)。
const DELIV_TARGET: Record<string, { tab: string; doc?: string }> = {
  prd: { tab: 'docs', doc: 'prd' },
  spec: { tab: 'docs', doc: 'spec' },
  code: { tab: 'code' },
  test_report: { tab: 'docs', doc: 'test_report' },
  // delivery: 无目标,只展示状态
}

// doc 类交付物(spec/test_report/prd)走 docApi.confirm(写 story_doc);
// 非 doc 类(code/delivery)走 deliverablesApi.confirm(写 context_json)。
const DOC_DELIVERABLES = new Set(['spec', 'test_report', 'prd'])

/**
 * StorySidebar — 左侧导航。
 *
 * 结构:
 *   - 返回
 *   - 模块导航(概览/代码/文档 tab)
 *   - 交付物导航:每项 = 一个交付物。
 *     确认用 checkbox(☑ 已确认 / ☐ 待确认);产物存在与否用图标明暗 + 小字说明。
 *     点击项 = 精准跳转(跳 tab + 打开对应 doc)。
 *   - gate 推进入口(进入下一状态):未满足置灰 + 下方红字显示缺什么
 *   - 底部操作(PRD 打开 / 归档)
 *
 * 确认模式选择(NN/g + Eleken 研究):用户主动确认完成 → checkbox 是正确模式
 * (deliberate commit);产物不存在(前置条件未满足)→ checkbox 置灰禁用 + 说明原因。
 */
export default function StorySidebar({
  storyKey, modules, activeModule, onModuleChange, onNavigate, onArchive, onBack, prdPath, onAdvance,
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
    if (target) onNavigate(target.tab, target.doc)
  }

  // gate 未满足时,缺失的成果物名(下方红字显示)。
  const gateMissing = gate && !gate.all_satisfied
    ? gate.required.filter((r) => !r.satisfied).map((r) => r.label)
    : []

  return (
    <aside className="story-sidebar">
      {onBack && (
        <button className="ss-back" onClick={onBack}>
          ← 返回
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

      {/* 交付物导航:checkbox 确认 + 图标明暗表产物 + 点击精准跳转 */}
      {deliverables.length > 0 && (
        <div className="ss-deliverables">
          <div className="ss-deliv-head">
            <span className="ss-deliv-title">📦 交付物</span>
          </div>
          <div className="ss-deliv-list">
            {deliverables.map((d) => {
              const skipped = !!d.skipped
              const showConfirm = !!d.needs_confirm && !skipped
              const target = DELIV_TARGET[d.key]
              const clickable = !!target && !skipped
              // checkbox 可用性:产物存在才能确认;无产物 → 置灰禁用。
              const confirmDisabled = !d.exists
              return (
                <div
                  key={d.key}
                  className={`ss-deliv-item${d.satisfied ? ' done' : ''}${skipped ? ' skipped' : ''}`}
                >
                  <div
                    className={`ss-deliv-main${clickable ? ' clickable' : ''}`}
                    title={target ? `查看${d.label}` : `${d.label}(无对应 tab)`}
                    onClick={() => clickable && handleDelivClick(d)}
                  >
                    <span className={`ss-deliv-icon${d.exists ? '' : ' absent'}`}>{d.icon}</span>
                    <span className="ss-deliv-label">{d.label}</span>
                    {/* 确认 checkbox:可点 = 确认动作;无产物置灰禁用 */}
                    {showConfirm && (
                      <input
                        type="checkbox"
                        className="ss-checkbox"
                        checked={!!d.confirmed}
                        disabled={confirmDisabled}
                        title={confirmDisabled ? '产物未生成,无法确认' : (d.confirmed ? '已确认' : '点击确认')}
                        onClick={(e) => e.stopPropagation()}
                        onChange={() => !confirmDisabled && handleConfirm(d.key)}
                      />
                    )}
                    {/* 跳过按钮(非 skipped 才显示) */}
                    {!skipped && (
                      <button
                        className="ss-deliv-skip"
                        title="跳过"
                        onClick={(e) => { e.stopPropagation(); handleSkip(d.key) }}
                      >
                        ⊘
                      </button>
                    )}
                  </div>
                  {/* 状态说明小字:产物存在与否 */}
                  {!skipped && (
                    <span className={`ss-deliv-status-text ${d.exists ? 'has' : 'absent'}`}>
                      {d.exists ? '有产物' : '无产物'}
                    </span>
                  )}
                  {skipped && (
                    <span className="ss-deliv-skipped-tag">⊘ 已跳过</span>
                  )}
                </div>
              )
            })}
          </div>

          {/* gate 推进入口:all_satisfied 可点;未满足置灰 + 下方红字显示缺什么 */}
          {gate && (
            <div className="ss-gate-wrap">
              <button
                className={`ss-gate-btn ${gate.all_satisfied ? 'ready' : 'locked'}`}
                disabled={!gate.all_satisfied}
                title={gate.all_satisfied ? `进入 ${gate.to}` : `还差: ${gateMissing.join('、')}`}
                onClick={onAdvance}
              >
                进入 {gate.to} →
              </button>
              {!gate.all_satisfied && gateMissing.length > 0 && (
                <div className="ss-gate-reason">还差: {gateMissing.join('、')}</div>
              )}
            </div>
          )}
        </div>
      )}

      <div className="ss-bottom-actions">
        {prdPath && (
          <button
            className="ss-prd-btn"
            title={prdPath}
            onClick={() => window.open(`file:///${prdPath.replace(/\\/g, '/')}`, '_blank', 'noopener,noreferrer')}
          >
            📄 打开 PRD
          </button>
        )}
        {onArchive && (
          <button className="ss-archive-btn" onClick={onArchive} title="已上线并验证过，归档此 Story">
            归档
          </button>
        )}
      </div>
    </aside>
  )
}
