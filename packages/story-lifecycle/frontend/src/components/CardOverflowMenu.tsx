import { useEffect, useRef, useState } from 'react'

/**
 * CardOverflowMenu — StoryCard 右上角「⋯」溢出菜单。
 *
 * 交互抄 MoreMenu(outside-click + Escape 关闭),但 items 是 action 回调(button)而非
 * NavLink(导航)。两个动作:移动生命周期态 + 删除(软删,可恢复)。
 * 触发器 onClick stopPropagation,避免冒泡到卡片整体跳详情页。
 */

// 5 个生命周期态(与后端 LifecycleState enum 对称),全开放可移。
const LIFECYCLE_STATES: { value: string; label: string }[] = [
  { value: '待启动', label: '待启动' },
  { value: '开发', label: '开发中' },
  { value: '测试', label: '测试中' },
  { value: '上线', label: '待上线' },
  { value: '结项', label: '已结项' },
]

export default function CardOverflowMenu({
  currentLifecycle,
  onMove,
  onDelete,
}: {
  currentLifecycle?: string | null
  onMove: (state: string) => void
  onDelete: () => void
}) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  return (
    <div className="card-overflow" ref={ref}>
      <button
        type="button"
        className={`card-overflow-trigger${open ? ' open' : ''}`}
        onClick={(e) => {
          e.stopPropagation()
          setOpen((v) => !v)
        }}
        aria-haspopup="menu"
        aria-expanded={open}
        title="更多操作"
      >
        ⋯
      </button>
      {open && (
        <div className="card-overflow-dropdown" role="menu" onClick={(e) => e.stopPropagation()}>
          <div className="card-overflow-section">移动到</div>
          {LIFECYCLE_STATES.map((s) => {
            const disabled = s.value === currentLifecycle
            return (
              <button
                key={s.value}
                type="button"
                className="card-overflow-item"
                disabled={disabled}
                onClick={() => {
                  setOpen(false)
                  onMove(s.value)
                }}
              >
                {s.label}
                {disabled && <span className="card-overflow-current">当前</span>}
              </button>
            )
          })}
          <div className="card-overflow-divider" />
          <button
            type="button"
            className="card-overflow-item card-overflow-danger"
            onClick={() => {
              setOpen(false)
              onDelete()
            }}
          >
            删除
          </button>
        </div>
      )}
    </div>
  )
}
