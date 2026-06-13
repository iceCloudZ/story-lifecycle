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
}

export default function StorySidebar({ storyKey, storyTitle, storyStatus, modules, activeModule, onModuleChange }: Props) {
  return (
    <aside className="story-sidebar">
      <div className="ss-story-info">
        <div className="ss-title" title={storyKey}>{storyTitle || storyKey}</div>
        <div className="ss-status">{storyStatus}</div>
      </div>
      <nav className="ss-nav">
        {modules.map((m) => (
          <button
            key={m.id}
            className={`ss-nav-item ${activeModule === m.id ? 'active' : ''}`}
            onClick={() => onModuleChange(m.id)}
          >
            <span className="ss-icon">{m.icon}</span>
            <span className="ss-label">{m.label}</span>
            {m.badge != null && (
              <span className={`ss-badge ${m.badgeVariant === 'danger' ? 'ss-badge-danger' : ''}`}>
                {m.badge}
              </span>
            )}
          </button>
        ))}
      </nav>
    </aside>
  )
}
