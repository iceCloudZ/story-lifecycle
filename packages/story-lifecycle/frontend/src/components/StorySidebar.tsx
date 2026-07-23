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

// 交付物 key → 点击跳转的 tab。doc 类(doc_type)→ docs;code → code;
// delivery 没有专门 tab → 留空(不可点,只显示状态)。
const DELIV_TARGET_TAB: Record<string, string> = {
  prd: 'docs',
  spec: 'docs',
  code: 'code',
  test_report: 'docs',
  // delivery: 无目标,只展示
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
 *   - 交付物导航:每项 = 一个交付物,带双灯(产物/确认),点击跳到对应 tab。
 *     灯状态直接嵌在导航项上,无需另开交付物区。确认灯可点 = 确认动作。
 *   - gate 推进入口(进入下一状态)收在交付物列表底
 *   - 底部操作(PRD 打开 / 归档)
 *
 * 标题/状态徽章已删(与 detail 头部重复)。
 */
export default function StorySidebar({
  storyKey, modules, activeModule, onModuleChange, onArchive, onBack, prdPath, onAdvance,
}: Props) {
  const qc = useQueryClient()
  const archived = false // 归档由 onArchive 触发,这里不依赖 status

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
    const tab = DELIV_TARGET_TAB[d.key]
    if (tab) onModuleChange(tab)
  }

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

      {/* 交付物导航:每项带双灯,点击跳转 tab */}
      {deliverables.length > 0 && (
        <div className="ss-deliverables">
          <div className="ss-deliv-head">
            <span className="ss-deliv-title">📦 交付物</span>
            <span className="ss-deliv-legend" title="左:产物是否存在 · 右:是否已确认">
              <span className="ss-lamp off" />
              <span className="ss-lamp off" />
            </span>
          </div>
          <div className="ss-deliv-list">
            {deliverables.map((d) => {
              const skipped = !!d.skipped
              const showConfirm = !!d.needs_confirm && !skipped
              const targetTab = DELIV_TARGET_TAB[d.key]
              const clickable = !!targetTab && !skipped
              return (
                <div
                  key={d.key}
                  className={`ss-deliv-item${d.satisfied ? ' done' : ''}${skipped ? ' skipped' : ''}`}
                >
                  <div
                    className={`ss-deliv-main${clickable ? ' clickable' : ''}`}
                    title={targetTab ? `查看${d.label}` : `${d.label}(无对应 tab)`}
                    onClick={() => clickable && handleDelivClick(d)}
                  >
                    <span className="ss-deliv-icon">{d.icon}</span>
                    <span className="ss-deliv-label">{d.label}</span>
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
                  {skipped ? (
                    <span className="ss-deliv-skipped-tag">⊘ 已跳过</span>
                  ) : (
                    <span className="ss-deliv-lamps">
                      {/* 产物灯:只读 */}
                      <span
                        className={`ss-lamp ${d.exists ? 'on' : 'off'}`}
                        title={`产物${d.exists ? '已存在' : '未生成'}`}
                      />
                      {/* 确认灯:可点击 → 确认动作 */}
                      {showConfirm && (
                        <button
                          className={`ss-lamp clickable ${d.confirmed ? 'on' : 'off'}`}
                          title={d.confirmed ? '已确认' : '点击确认'}
                          onClick={(e) => { e.stopPropagation(); handleConfirm(d.key) }}
                        />
                      )}
                    </span>
                  )}
                </div>
              )
            })}
          </div>

          {/* gate 推进入口:all_satisfied 可点,否则置灰 + tooltip */}
          {gate && (
            <button
              className={`ss-gate-btn ${gate.all_satisfied ? 'ready' : 'locked'}`}
              disabled={!gate.all_satisfied}
              title={
                gate.all_satisfied
                  ? `进入 ${gate.to}`
                  : `还差: ${gate.required.filter((r) => !r.satisfied).map((r) => r.label).join('、')}`
              }
              onClick={onAdvance}
            >
              进入 {gate.to} →
            </button>
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
        {onArchive && !archived && (
          <button className="ss-archive-btn" onClick={onArchive} title="已上线并验证过，归档此 Story">
            归档
          </button>
        )}
      </div>
    </aside>
  )
}
