import { useState, useEffect, useCallback } from 'react'
import TerminalPanel from './TerminalPanel'
import './TerminalTab.css'

interface Props {
  storyKey: string
  status?: string
}

interface Session {
  session_id: string
  adapter: string
  stage: string
  model: string
  status: string
  started_at: string
}

export default function TerminalTab({ storyKey }: Props) {
  const [sessions, setSessions] = useState<Session[]>([])
  const [activeSession, setActiveSession] = useState<string | null>(null)

  const fetchSessions = useCallback(async () => {
    try {
      const r = await fetch(`/api/story/${storyKey}/sessions`)
      if (r.ok) {
        const data = await r.json()
        setSessions(data.sessions || [])
      }
    } catch { /* API may not exist yet */ }
  }, [storyKey])

  // Poll sessions on mount + every 5s. fetchSessions' setState runs after an
  // awaited fetch, so it is not synchronous in this effect body.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    fetchSessions()
    const interval = setInterval(fetchSessions, 5000)
    return () => clearInterval(interval)
  }, [fetchSessions])

  async function handleSpawn() {
    const r = await fetch(`/api/story/${storyKey}/sessions/spawn`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ adapter: 'claude', model: '' }),
    })
    if (r.ok) {
      const data = await r.json()
      setActiveSession(data.session_id)
      fetchSessions()
    } else {
      // Fallback: try legacy single-PTY spawn
      const r2 = await fetch(`/api/pty/${storyKey}/spawn`, { method: 'POST' })
      if (r2.ok) {
        const data = await r2.json()
        setActiveSession(data.session_id || storyKey)
        fetchSessions()
      }
    }
  }

  // Auto-select first running session, or the first session if none running
  const activeSessionId =
    activeSession ||
    (sessions.length > 0
      ? (sessions.find((s) => s.status === 'running') || sessions[0]).session_id
      : null)

  return (
    <div className="tab-content terminal-tab">
      {/* Session tabs */}
      <div className="tt-session-tabs">
        {sessions.map((s) => (
          <button
            key={s.session_id}
            className={`tt-session-tab ${s.session_id === activeSessionId ? 'active' : ''}`}
            onClick={() => setActiveSession(s.session_id)}
          >
            <span className="tt-adapter-icon">
              {s.adapter === 'claude' ? '🟠' : '🟢'}
            </span>
            <span className="tt-session-label">
              {s.adapter}
            </span>
            <span className={`tt-status-dot tt-${s.status === 'running' ? 'running' : 'exited'}`} />
          </button>
        ))}
        <button className="tt-session-tab tt-spawn-btn" onClick={handleSpawn} title="新建会话">
          + 新建
        </button>
      </div>

      {/* Active terminal */}
      {activeSessionId ? (
        <div className="tt-terminal-area">
          <TerminalPanel storyKey={storyKey} sessionId={activeSessionId} autoConnect />
        </div>
      ) : (
        <div className="tt-no-session">
          <p>暂无 CLI 会话</p>
          <button className="btn btn-primary" onClick={handleSpawn}>
            启动终端
          </button>
        </div>
      )}
    </div>
  )
}
