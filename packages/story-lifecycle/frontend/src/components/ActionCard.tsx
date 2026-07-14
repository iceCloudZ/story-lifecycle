import type { AgentAction } from '../api/client'

const ADAPTER_ICON: Record<string, string> = {
  claude: '🟠',
  codex: '🟢',
  kimi: '🔵',
}

const ADAPTERS = ['claude', 'codex', 'kimi']

interface Props {
  action: AgentAction
  index: number
  editable?: boolean
  onAdapterChange?: (index: number, adapter: string) => void
}

export default function ActionCard({ action, index, editable, onAdapterChange }: Props) {
  if (action.action === 'skip') {
    return (
      <div className="action-card action-skip">
        <div className="ac-header">
          <span className="ac-index">#{index + 1}</span>
          <span className="ac-icon">⏭️</span>
          <span className="ac-stage">{action.stage}</span>
          <span className="ac-badge ac-skip-badge">SKIP</span>
        </div>
        <div className="ac-reason">{action.reason}</div>
      </div>
    )
  }

  return (
    <div className="action-card action-launch">
      <div className="ac-header">
        <span className="ac-index">#{index + 1}</span>
        <span className="ac-icon">{ADAPTER_ICON[action.adapter ?? 'claude'] ?? '🔧'}</span>
        <span className="ac-stage">{action.stage}</span>
        {editable && onAdapterChange ? (
          <select
            className="ac-adapter-select"
            value={action.adapter ?? 'claude'}
            onChange={(e) => onAdapterChange(index, e.target.value)}
          >
            {ADAPTERS.map((a) => (
              <option key={a} value={a}>
                {ADAPTER_ICON[a]} {a}
              </option>
            ))}
          </select>
        ) : (
          <span className="ac-badge ac-adapter-badge">{action.adapter}</span>
        )}
      </div>
      {action.focus && <div className="ac-focus">{action.focus}</div>}
    </div>
  )
}
