import { useEffect, useRef, useState } from 'react'
import { NavLink } from 'react-router-dom'

interface MoreMenuProps {
  sections: {
    label?: string
    items: { to: string; text: string; end?: boolean }[]
  }[]
}

export default function MoreMenu({ sections }: MoreMenuProps) {
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
    <div className="more-menu" ref={ref}>
      <button
        className={`more-menu-trigger${open ? ' open' : ''}`}
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open}
      >
        更多 ▾
      </button>
      {open && (
        <div className="more-menu-dropdown" role="menu">
          {sections.map((section, i) => (
            <div key={i}>
              {section.label && <div className="more-menu-section">{section.label}</div>}
              {section.items.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.end}
                  className={({ isActive }) =>
                    isActive ? 'more-menu-item active' : 'more-menu-item'
                  }
                  onClick={() => setOpen(false)}
                >
                  {item.text}
                </NavLink>
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
