import { useStories } from '../hooks/useStories'
import type { StorySummary } from '../store/storyStore'
import { TYPE_LABELS, DONE_STATUSES } from './tapdMeta'
import './lifecycle/LifecyclePage.css'
import './CalendarPage.css'

/**
 * 日历 — 按截止日期展示当月 TAPD 子任务(从 Dashboard 的日历 tab 抽出)。
 */
export default function CalendarPage() {
  const { stories: allStories } = useStories()
  // 日历视图需要子任务数据，保留所有 TAPD 来源
  const tapdStories = allStories.filter((s) => s.tapdType)

  return (
    <div className="lifecycle-page">
      <div className="lifecycle-header">
        <h2>日历</h2>
      </div>
      <CalendarView stories={tapdStories} />
    </div>
  )
}

// ---- Calendar view ----

function CalendarView({ stories }: { stories: StorySummary[] }) {
  const now = new Date()
  const year = now.getFullYear()
  const month = now.getMonth()
  const today = now.toISOString().slice(0, 10)

  const byDate: Record<string, StorySummary[]> = {}
  for (const s of stories) {
    if (s.tapdType !== 'subtask') continue
    const st = s.tapdStatus || ''
    if (DONE_STATUSES.has(st)) continue
    const dl = (s.deadline || '').slice(0, 10)
    if (!dl) continue
    if (!byDate[dl]) byDate[dl] = []
    byDate[dl].push(s)
  }

  const firstDay = new Date(year, month, 1)
  const lastDay = new Date(year, month + 1, 0)
  const startPad = firstDay.getDay()
  const days: string[] = []
  for (let i = 0; i < startPad; i++) days.push('')
  for (let d = 1; d <= lastDay.getDate(); d++) {
    const ds = `${year}-${String(month + 1).padStart(2, '0')}-${String(d).padStart(2, '0')}`
    days.push(ds)
  }

  const dayNames = ['日', '一', '二', '三', '四', '五', '六']

  return (
    <div className="calendar-view">
      <h3 className="cal-title">{year}年{month + 1}月</h3>
      <div className="cal-grid">
        {dayNames.map((n) => (
          <div key={n} className="cal-day-header">{n}</div>
        ))}
        {days.map((ds, i) => {
          const items = ds ? (byDate[ds] || []) : []
          const isToday = ds === today
          const d = ds ? parseInt(ds.slice(8)) : 0
          return (
            <div key={ds || `empty-${i}`} className={`cal-day ${isToday ? 'cal-today' : ''} ${ds ? '' : 'cal-empty'}`}>
              {ds && <div className="cal-date">{d}</div>}
              {items.slice(0, 6).map((s) => {
                const typeInfo = TYPE_LABELS[s.tapdType || '']
                return (
                  <div key={s.storyKey} className="cal-task" title={s.title}>
                    <span className="cal-task-dot" style={{ background: typeInfo?.color || '#7c3aed' }} />
                    <span className="cal-task-text">{(s.title || '').slice(0, 16)}</span>
                    {s.tapdUrl && (
                      <a className="cal-task-link" href={s.tapdUrl} target="_blank" rel="noopener noreferrer"
                        onClick={(e) => e.stopPropagation()} title="在 TAPD 中查看">&#x2197;</a>
                    )}
                  </div>
                )
              })}
              {items.length > 6 && <div className="cal-more">+{items.length - 6}</div>}
            </div>
          )
        })}
      </div>
    </div>
  )
}
