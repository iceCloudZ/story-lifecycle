import { useState, useEffect, useCallback } from 'react'
import TerminalPanel from './TerminalPanel'
import DialogHistory from './DialogHistory'
import './TerminalTab.css'

interface Props {
  storyKey: string
  status?: string
  /** 进度条 stage chip 点击后传入:只显示该 stage 的 session,并优先选中它。 */
  stage?: string
}

interface Session {
  session_id: string
  adapter: string
  stage: string
  model: string
  status: string
  started_at: string
}

// 可恢复状态:删了概览的「继续执行」横幅后,恢复入口收到这里。
// paused/failed/blocked/aborted 都调 PUT /advance 重启全自动编排循环。
const RESUMABLE = new Set(['paused', 'failed', 'blocked', 'aborted'])

export default function TerminalTab({ storyKey, status, stage }: Props) {
  const [sessions, setSessions] = useState<Session[]>([])
  const [activeSession, setActiveSession] = useState<string | null>(null)
  const [resuming, setResuming] = useState(false)
  const [showHistory, setShowHistory] = useState(true)

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

  // stage prop 变化时(进度条 chip 点击),优先选中该 stage 的会话。
  useEffect(() => {
    if (!stage) return
    const match = sessions.find((s) => s.stage === stage)
    if (match) setActiveSession(match.session_id)
  }, [stage, sessions])

  async function handleSpawn() {
    // adapter 留空 → 后端 resolve_stage_adapter 从 _agent_actions 拿用户在 plan UI
    // 选的 adapter(老逻辑硬编码 claude,导致 plan 改 kimi 这里还 spawn claude)。
    const r = await fetch(`/api/story/${storyKey}/sessions/spawn`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ adapter: '', model: '' }),
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

  // 恢复全自动编排(paused/failed → PUT /advance 重启循环)。
  // 成功后刷新 sessions,新 spawn 的 driver 会话会出现在列表里。
  async function handleResume() {
    setResuming(true)
    try {
      const r = await fetch(`/api/story/${storyKey}/advance`, { method: 'PUT' })
      if (r.ok) {
        fetchSessions()
      } else {
        alert(`恢复失败: ${(await r.json()).detail || '未知错误'}`)
      }
    } catch {
      alert('恢复失败: 网络错误')
    } finally {
      setResuming(false)
    }
  }

  // stage 过滤:有 stage prop 时只显示该 stage 的 session;否则全部。
  const visibleSessions = stage
    ? sessions.filter((s) => s.stage === stage)
    : sessions

  // Auto-select only a RUNNING session. Never auto-pick an exited session,
  // because that causes the terminal to reconnect to a dead PTY forever.
  const activeSessionId =
    activeSession ||
    visibleSessions.find((s) => s.status === 'running')?.session_id ||
    null

  // 当前选中会话的 stage(喂给 DialogHistory,只看该 stage 的历史)。
  const activeStage = sessions.find((s) => s.session_id === activeSessionId)?.stage || stage || ''
  const resumable = RESUMABLE.has(status ?? '')

  return (
    <div className="tab-content terminal-tab">
      {/* Session tabs(每个标注 stage · adapter)+ 新建 + 恢复 */}
      <div className="tt-session-tabs">
        {visibleSessions.map((s) => (
          <button
            key={s.session_id}
            className={`tt-session-tab ${s.session_id === activeSessionId ? 'active' : ''} ${s.status !== 'running' ? 'tt-exited' : ''}`}
            onClick={() => setActiveSession(s.session_id)}
            title={s.status === 'running' ? '运行中' : '已退出'}
          >
            <span className="tt-adapter-icon">
              {s.adapter === 'claude' ? '🟠' : '🟢'}
            </span>
            <span className="tt-session-label">
              {s.stage ? `${s.stage} · ${s.adapter}` : s.adapter}
            </span>
            <span className={`tt-status-dot tt-${s.status === 'running' ? 'running' : 'exited'}`} />
          </button>
        ))}
        <button className="tt-session-tab tt-spawn-btn" onClick={handleSpawn} title="新建会话">
          + 新建
        </button>
        {resumable && (
          <button
            className="tt-resume-btn"
            onClick={handleResume}
            disabled={resuming}
            title="恢复全自动编排循环(PUT /advance)"
          >
            {resuming ? '恢复中…' : '▶ 恢复执行'}
          </button>
        )}
        <button
          className="tt-history-toggle"
          onClick={() => setShowHistory((v) => !v)}
          title={showHistory ? '隐藏对话历史' : '显示对话历史'}
        >
          {showHistory ? '📜 隐藏历史' : '📜 对话历史'}
        </button>
      </div>

      {/* 终端 + 对话历史(左右分栏;历史可收起) */}
      <div className={`tt-main${showHistory ? ' with-history' : ''}`}>
        <div className="tt-terminal-pane">
          {activeSessionId ? (
            <TerminalPanel storyKey={storyKey} sessionId={activeSessionId} autoConnect />
          ) : (
            <div className="tt-no-session">
              <p>{stage ? `${stage} 阶段没有 CLI 会话` : '当前没有运行中的 CLI 会话'}</p>
              {visibleSessions.length > 0 && (
                <p className="tt-hint">点击上方历史会话可查看最终输出,或启动新会话继续工作。</p>
              )}
              <button className="btn btn-primary" onClick={handleSpawn}>
                启动终端
              </button>
            </div>
          )}
        </div>
        {showHistory && (
          <div className="tt-history-pane">
            <DialogHistory storyKey={storyKey} stage={activeStage} />
          </div>
        )}
      </div>
    </div>
  )
}
