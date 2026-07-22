import type { ReactNode } from 'react'
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
  storyTitle: string
  storyStatus: string
  modules: Module[]
  activeModule: string
  onModuleChange: (id: string) => void
  onArchive?: () => void
  onBack?: () => void
}

// 线性图标(16px,stroke=currentColor),按 module id 取;取不到回落 emoji。
const LINE_ICONS: Record<string, ReactNode> = {
  overview: (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4">
      <rect x="1.5" y="1.5" width="5.5" height="5.5" rx="1.2" />
      <rect x="9" y="1.5" width="5.5" height="5.5" rx="1.2" />
      <rect x="1.5" y="9" width="5.5" height="5.5" rx="1.2" />
      <rect x="9" y="9" width="5.5" height="5.5" rx="1.2" />
    </svg>
  ),
  terminal: (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
      <rect x="1.5" y="2.5" width="13" height="11" rx="1.5" />
      <path d="M4.5 6l2.5 2.5L4.5 11" />
      <path d="M8.5 11h3" />
    </svg>
  ),
  code: (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="4.5" cy="4" r="2" />
      <circle cx="4.5" cy="12" r="2" />
      <circle cx="11.5" cy="7.5" r="2" />
      <path d="M4.5 6v4" />
      <path d="M11.5 9.5c0 1.5-2 2.5-4.5 2.5" />
    </svg>
  ),
  docs: (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
      <path d="M4 1.5h5.5L12.5 4.5V14.5h-8.5z" />
      <path d="M9.5 1.5v3h3" />
      <path d="M6 8h4M6 10.5h4" />
    </svg>
  ),
}

// status → badge 样式类(复用全局 .badge-*)
const STATUS_BADGE: Record<string, string> = {
  active: 'badge-active',
  planning: 'badge-completed',
  paused: 'badge-paused',
  blocked: 'badge-blocked',
  failed: 'badge-failed',
  completed: 'badge-completed',
  archived: 'badge-aborted',
  aborted: 'badge-aborted',
  idle: 'badge-aborted',
}

export default function StorySidebar({ storyKey, storyTitle, storyStatus, modules, activeModule, onModuleChange, onArchive, onBack }: Props) {
  const archived = storyStatus === 'archived'
  return (
    <aside className="story-sidebar">
      {onBack && (
        <button className="ss-back" onClick={onBack}>
          ← 返回
        </button>
      )}
      <div className="ss-story-info">
        <div className="ss-title" title={storyKey}>{storyTitle || storyKey}</div>
        <span className={`badge ${STATUS_BADGE[storyStatus] ?? 'badge-aborted'}`}>
          {storyStatus}
        </span>
      </div>
      <nav className="ss-nav">
        {modules.map((m) => (
          <button
            key={m.id}
            className={`ss-nav-item ${activeModule === m.id ? 'active' : ''}`}
            onClick={() => onModuleChange(m.id)}
          >
            <span className="ss-icon">{LINE_ICONS[m.id] ?? m.icon}</span>
            <span className="ss-label">{m.label}</span>
            {m.badge != null && (
              <span className={`ss-badge ${m.badgeVariant === 'danger' ? 'ss-badge-danger' : ''}`}>
                {m.badge}
              </span>
            )}
          </button>
        ))}
      </nav>
      {onArchive && !archived && (
        <div className="ss-archive">
          <button className="ss-archive-btn" onClick={onArchive} title="已上线并验证过，归档此 Story">
            归档
          </button>
        </div>
      )}
    </aside>
  )
}
