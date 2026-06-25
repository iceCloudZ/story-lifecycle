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
}

export default function StorySidebar({ storyKey, storyTitle, storyStatus, modules, activeModule, onModuleChange, onArchive }: Props) {
  const archived = storyStatus === 'archived'
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
      {onArchive && !archived && (
        <div className="ss-archive">
          <button className="btn btn-archive" onClick={onArchive} title="已上线并验证过，归档此 Story">
            🗃️ 归档
          </button>
        </div>
      )}
    </aside>
  )
}
