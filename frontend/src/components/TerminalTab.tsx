import usePTYSessions from '../hooks/usePTYSessions'
import TerminalPanel from './TerminalPanel'
import './TerminalTab.css'

interface Props {
  storyKey: string
  status: string
}

export default function TerminalTab({ storyKey, status }: Props) {
  const { sessions, activeSessionId, setActiveSession } =
    usePTYSessions({ storyKey, autoConnect: status === 'active' })

  return (
    <div className="tab-content terminal-tab">
      {/* Session tabs */}
      <div className="tt-session-tabs">
        {sessions.map((s) => (
          <button
            key={s.sessionId}
            className={`tt-session-tab ${s.sessionId === activeSessionId ? 'active' : ''}`}
            onClick={() => setActiveSession(s.sessionId)}
          >
            <span className="tt-adapter-icon">
              {s.adapter === 'claude' ? '🟠' : '🟢'}
            </span>
            <span className="tt-session-label">
              {s.stage} · {s.adapter}
            </span>
            <span className={`tt-status-dot tt-${s.status}`} />
          </button>
        ))}
        {sessions.length === 0 && (
          <div className="tt-empty-sessions">暂无 CLI 会话</div>
        )}
      </div>

      {/* Active terminal */}
      {activeSessionId ? (
        <div className="tt-terminal-area">
          <TerminalPanel storyKey={activeSessionId} autoConnect />
          <div className="tt-session-info">
            {sessions.filter(s => s.sessionId === activeSessionId).map(s => (
              <span key={s.sessionId}>会话: {s.sessionId} | 启动: {s.startedAt}</span>
            ))}
          </div>
        </div>
      ) : (
        <div className="tt-no-session">选择或启动一个终端会话</div>
      )}
    </div>
  )
}
