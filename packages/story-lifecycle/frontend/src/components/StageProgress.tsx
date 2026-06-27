import './StageProgress.css'

interface Stage {
  name: string
  status: 'completed' | 'running' | 'pending' | 'failed' | 'skipped'
}

interface Props {
  stages: Stage[]
  currentStage?: string
}

export default function StageProgress({ stages, currentStage }: Props) {
  return (
    <div className="stage-progress">
      <div className="sp-track">
        {stages.map((s, i) => {
          const isActive = s.name === currentStage
          const state = isActive ? 'running' : s.status
          return (
            <div key={s.name} className={`sp-step sp-${state}`}>
              {i > 0 && <div className="sp-line" />}
              <div className="sp-dot" />
              <div className="sp-label">
                <span className="sp-name">{s.name}</span>
                <span className="sp-status-text">{state}</span>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
