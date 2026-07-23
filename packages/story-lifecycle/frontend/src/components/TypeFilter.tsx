import { TYPE_LABELS } from '../pages/tapdMeta'

/**
 * TypeFilter — 生命周期列表页共用的类型筛选条(需求/缺陷/子任务)。
 *
 * 4 个生命周期列表页都按 lifecycleState 过滤,tapd_type 不分,需求和缺陷会混在一起
 * (默认视图看不出哪些是 bug)。这里加个客户端类型筛选下拉,值走 tapdType。
 * 标签/配色复用 tapdMeta 的 TYPE_LABELS,跟 BugsPage 的 .filter-select 风格一致。
 */
export default function TypeFilter({
  value,
  onChange,
}: {
  value: string
  onChange: (v: string) => void
}) {
  return (
    <div className="lifecycle-filters">
      <select
        className="filter-select"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      >
        <option value="">所有类型</option>
        {Object.entries(TYPE_LABELS).map(([k, v]) => (
          <option key={k} value={k}>
            {v.label}
          </option>
        ))}
      </select>
      {value && (
        <button type="button" className="btn" onClick={() => onChange('')}>
          重置
        </button>
      )}
    </div>
  )
}
