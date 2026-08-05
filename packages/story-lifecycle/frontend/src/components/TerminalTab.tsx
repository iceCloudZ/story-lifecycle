import { useState, useEffect, useCallback } from 'react'
import type { AgentAction, PlanStage } from '../api/client'
import TerminalPanel from './TerminalPanel'
import DialogHistory from './DialogHistory'
import './TerminalTab.css'

const ADAPTER_ICON: Record<string, string> = {
  claude: '🟠',
  codex: '🟢',
  kimi: '🔵',
  opencode: '🟣',
}
const ADAPTERS = ['claude', 'codex', 'kimi', 'opencode']

interface Props {
  storyKey: string
  status?: string
  /** 进度条 stage chip 点击后传入:只显示该 stage 的 session,并优先选中它。 */
  stage?: string
  /** 下沉的 plan 数据(有 plan 时终端区切 stage 卡片视图)。 */
  actions?: AgentAction[]
  stages?: PlanStage[]
  currentStage?: string
  isConfirmed?: boolean
  /** adapter 下拉是否可切(planning 且未确认时)。 */
  editable?: boolean
  onAdapterChange?: (stage: string, adapter: string) => void
  onConfirmPlan?: () => void
  onRegeneratePlan?: () => void
}

// status 只接受 running/exited — 后端 Step 3 后不再吐 DB 静态 active/completed
// (DESIGN-session-pty-id-model.md §3.7)。联合类型防 status 词汇漂移。
type SessionStatus = 'running' | 'exited'
interface Session {
  session_id: string
  /** WS attach 凭据:活 PTY 的注册表 id。kimi 运行期 DB sid 未捕获(""),靠它 attach。 */
  attach_id: string
  adapter: string
  stage: string
  model: string
  status: SessionStatus
  started_at: string
  /** 设计12 改动3:stage 完成摘要(judge_stage_completion 的 summary)。 */
  completion_summary?: string
}

// chip 的稳定 key:kimi 运行期 DB sid 为空,用 attach_id 兜底。
const chipKeyOf = (s: Session) =>
  s.session_id || s.attach_id || `${s.stage}-${s.adapter}`

// 可恢复状态:删了概览的「继续执行」横幅后,恢复入口收到这里。
// paused/failed/blocked/aborted 都调 PUT /advance 重启全自动编排循环。
const RESUMABLE = new Set(['paused', 'failed', 'blocked', 'aborted'])

interface StagePrompt { stage: string; path: string; content: string }
interface PromptsResponse { story_key: string; prompts: StagePrompt[] }

export default function TerminalTab({
  storyKey, status, stage,
  actions = [], stages = [], currentStage, isConfirmed, editable,
  onAdapterChange, onConfirmPlan, onRegeneratePlan,
}: Props) {
  const [sessions, setSessions] = useState<Session[]>([])
  // 选中的会话 chip(chipKeyOf 值):running → attach;exited → 展示 resume 入口。
  const [selectedKey, setSelectedKey] = useState<string | null>(null)
  const [resuming, setResuming] = useState(false)
  // 默认隐藏历史:全宽留给终端,需要时点「对话历史」展开。
  const [showHistory, setShowHistory] = useState(false)
  // stage 卡片视图:当前选中 stage(默认 currentStage,fallback 首个)。
  const [activeStage, setActiveStage] = useState<string>(
    currentStage || stages[0]?.name || ''
  )
  const [spawning, setSpawning] = useState(false)
  const [copied, setCopied] = useState(false)

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

  // currentStage 变化(后端推进 stage)时同步选中 chip。
  useEffect(() => {
    if (currentStage && stages.some((s) => s.name === currentStage)) {
      setActiveStage(currentStage)
    }
  }, [currentStage, stages])

  // 无 plan 视图:stage prop 变化时优先选中该 stage 的会话。
  // (这个 effect 放在顶部,不能放在下面的条件 return 之后 —— Hooks 规则。)
  useEffect(() => {
    if (!stage) return
    const match = sessions.find((s) => s.stage === stage)
    if (match) setSelectedKey(chipKeyOf(match))
  }, [stage, sessions])

  async function handleSpawn() {
    // adapter 留空 → 后端 resolve_stage_adapter 从 _agent_actions 拿用户在 plan UI
    // 选的 adapter(老逻辑硬编码 claude,导致 plan 改 kimi 这里还 spawn claude)。
    // 后端对活 PTY 有复用检查:已存在会话时返回现有 id(reused),不起重复进程。
    setSpawning(true)
    try {
      const r = await fetch(`/api/story/${storyKey}/sessions/spawn`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ adapter: '', model: '' }),
      })
      if (r.ok) {
        const data = await r.json()
        setSelectedKey(data.session_id)
        fetchSessions()
      } else {
        const detail = await r.json().then((b) => b?.detail).catch(() => '') || r.statusText
        // Fallback: try legacy single-PTY spawn
        const r2 = await fetch(`/api/pty/${storyKey}/spawn`, { method: 'POST' })
        if (r2.ok) {
          const data2 = await r2.json()
          setSelectedKey(data2.session_id || storyKey)
          fetchSessions()
        } else {
          // 此前失败被静默吞掉(「启动终端没反应」),必须给用户可见反馈。
          alert(`启动终端失败: ${detail}`)
        }
      }
    } catch (e) {
      alert(`启动终端失败: ${e instanceof Error ? e.message : e}`)
    } finally {
      setSpawning(false)
    }
  }

  // 半自动:复制选中 stage 的组装提示词(回落到上下文资料包)。
  async function handleCopyPrompt() {
    let text = ''
    try {
      const r = await fetch(`/api/story/${storyKey}/prompts`)
      if (r.ok) {
        const data: PromptsResponse = await r.json()
        text = (data.prompts || []).find((p) => p.stage === activeStage)?.content || ''
      }
    } catch { /* fall through to pack */ }
    if (!text) {
      const r = await fetch(`/api/story/${storyKey}/context/pack`)
      const body = await r.json()
      text = body.content || ''
    }
    if (!text) return
    await navigator.clipboard.writeText(text)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
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

  const resumable = RESUMABLE.has(status ?? '')

  // ============ 有 plan → stage 卡片视图 ============
  if (actions.length > 0 || stages.length > 0) {
    const planStages = stages.length > 0 ? stages.map((s) => s.name) : actions.map((a) => a.stage || '')
    const sel = activeStage && planStages.includes(activeStage) ? activeStage : (planStages[0] || '')
    const action = actions.find((a) => a.stage === sel)
    const stageMeta = stages.find((s) => s.name === sel)
    const isDone = !!stageMeta?.done
    const focus = action?.focus || stageMeta?.focus || ''
    const adapter = action?.adapter || stageMeta?.adapter || 'claude'
    // stage 级「启动前能切、启动后锁」:该 stage 有任何 session 就锁。
    const stageSessions = sessions.filter((s) => s.stage === sel)
    const hasSession = stageSessions.length > 0
    const canEditAdapter = !!editable && !!onAdapterChange && !hasSession
    // 设计12 改动3:该 stage 最近的完成摘要(重跑过取最新一条)。
    const stageSummary =
      [...stageSessions].reverse().find((s) => s.completion_summary)?.completion_summary || ''

    // 该 stage 选中 session:attach 该 stage 的活 PTY(用 attach_id —— kimi 运行期
    // DB sid 未捕获为空,注册表 id 才是 WS 凭据)。
    const stageActiveSessionId =
      stageSessions.find((s) => s.status === 'running' && s.attach_id)?.attach_id ||
      null

    const showConfirm = status === 'planning' && !isConfirmed

    return (
      <div className="tab-content terminal-tab">
        {/* stage chip 栏 + 规划确认按钮 */}
        <div className="tt-stage-bar">
          <div className="tt-stage-chips">
            {planStages.map((name) => {
              const meta = stages.find((s) => s.name === name)
              return (
                <button
                  key={name}
                  className={`tt-stage-chip${name === sel ? ' active' : ''}${meta?.done ? ' done' : ''}`}
                  onClick={() => setActiveStage(name)}
                >
                  {name}
                  {meta?.done && <span className="tt-stage-check">✓</span>}
                </button>
              )
            })}
          </div>
          {showConfirm && (
            <div className="tt-plan-actions">
              <button className="btn btn-sm" onClick={onRegeneratePlan}>重新规划</button>
              <button className="btn btn-sm btn-primary" onClick={onConfirmPlan}>确认规划，开始执行</button>
            </div>
          )}
        </div>

        {/* 选中 stage 的面板 */}
        <div className="tt-stage-panel">
          <div className="tt-stage-head">
            <div className="tt-stage-title">
              <span className="tt-stage-name">{sel}</span>
              {canEditAdapter ? (
                <select
                  className="tt-adapter-select"
                  value={adapter}
                  onChange={(e) => onAdapterChange?.(sel, e.target.value)}
                >
                  {ADAPTERS.map((a) => (
                    <option key={a} value={a}>{ADAPTER_ICON[a]} {a}</option>
                  ))}
                </select>
              ) : (
                <span className="tt-adapter-badge">{ADAPTER_ICON[adapter] ?? '🔧'} {adapter}</span>
              )}
              {isDone && <span className="tt-done-badge">已完成</span>}
            </div>
            <div className="tt-stage-ops">
              {resumable && (
                <button className="btn btn-sm" onClick={handleResume} disabled={resuming}>
                  {resuming ? '恢复中…' : '▶ 恢复执行'}
                </button>
              )}
              <button className="btn btn-sm" onClick={handleCopyPrompt}>
                {copied ? '已复制' : '复制提示词'}
              </button>
              {!isDone && !hasSession && (
                <button className="btn btn-sm btn-primary" onClick={handleSpawn} disabled={spawning}>
                  {spawning ? '启动中…' : '▶ 启动 CLI'}
                </button>
              )}
              <button
                className="tt-history-toggle"
                onClick={() => setShowHistory((v) => !v)}
                title={showHistory ? '隐藏对话历史' : '显示对话历史'}
              >
                {showHistory ? '隐藏历史' : '对话历史'}
              </button>
            </div>
          </div>

          {focus && <div className="tt-stage-focus">📋 {focus}</div>}

          {/* 设计12 改动3:stage 完成摘要(judge_stage_completion 的 summary) */}
          {isDone && stageSummary && (
            <div className="tt-stage-summary">
              <svg
                className="tt-stage-summary-icon"
                viewBox="0 0 16 16"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.5"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <path d="M3 8.5 6.5 12 13 4.5" />
              </svg>
              <span className="tt-stage-summary-text">{stageSummary}</span>
            </div>
          )}

          {/* 终端 + 对话历史 */}
          <div className={`tt-main${showHistory ? ' with-history' : ''}`}>
            <div className="tt-terminal-pane">
              {stageActiveSessionId ? (
                <TerminalPanel storyKey={storyKey} sessionId={stageActiveSessionId} autoConnect />
              ) : (
                <div className="tt-no-session">
                  <p>{sel ? `${sel} 阶段没有 CLI 会话` : '当前没有运行中的 CLI 会话'}</p>
                  {hasSession ? (
                    <p className="tt-hint">该阶段已有历史会话,点上方「恢复执行」继续。</p>
                  ) : (
                    <p className="tt-hint">点「启动 CLI」为该阶段开一个会话。</p>
                  )}
                </div>
              )}
            </div>
            {showHistory && (
              <div className="tt-history-pane">
                <DialogHistory storyKey={storyKey} stage={sel} />
              </div>
            )}
          </div>
        </div>
      </div>
    )
  }

  // ============ 无 plan → 现有 session tab 视图 ============
  // stage 过滤:有 stage prop 时只显示该 stage 的 session;否则全部。
  const visibleSessions = stage
    ? sessions.filter((s) => s.stage === stage)
    : sessions

  // 选中 chip;无选中时回落到 running 会话(auto-attach)。从不自动选 exited
  // 会话 —— 那会让终端永远重连一个死 PTY。
  const selected =
    visibleSessions.find((s) => chipKeyOf(s) === selectedKey) || null
  const runningSession =
    visibleSessions.find((s) => s.status === 'running' && s.attach_id) || null
  const effective = selected || runningSession
  // attach 目标:仅 running 会话可 attach(attach_id 是活 PTY 的注册表 id)。
  const activeSessionId =
    effective && effective.status === 'running' ? effective.attach_id : null
  const exitedSession = effective && effective.status !== 'running' ? effective : null

  // 当前选中会话的 stage(喂给 DialogHistory,只看该 stage 的历史)。
  const noPlanActiveStage = effective?.stage || stage || ''

  return (
    <div className="tab-content terminal-tab">
      {/* Session tabs(每个标注 stage · adapter)+ 新建 + 恢复 */}
      <div className="tt-session-tabs">
        {visibleSessions.map((s) => (
          <button
            key={chipKeyOf(s)}
            className={`tt-session-tab ${effective && chipKeyOf(s) === chipKeyOf(effective) ? 'active' : ''} ${s.status !== 'running' ? 'tt-exited' : ''}`}
            onClick={() => setSelectedKey(chipKeyOf(s))}
            title={s.status === 'running' ? '运行中,点击接入' : (s.session_id ? '已退出,点击可恢复会话' : '已退出,无可恢复 id')}
          >
            <span className={`tt-adapter-dot ${s.adapter === 'claude' ? 'claude' : 'kimi'}`} />
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
          {showHistory ? '隐藏历史' : '对话历史'}
        </button>
      </div>

      {/* 终端 + 对话历史(左右分栏;历史可收起) */}
      <div className={`tt-main${showHistory ? ' with-history' : ''}`}>
        <div className="tt-terminal-pane">
          {activeSessionId ? (
            <TerminalPanel storyKey={storyKey} sessionId={activeSessionId} autoConnect />
          ) : exitedSession ? (
            <div className="tt-no-session">
              <p>{exitedSession.stage ? `${exitedSession.stage} · ${exitedSession.adapter}` : exitedSession.adapter} 会话已退出</p>
              {exitedSession.session_id ? (
                <>
                  <p className="tt-hint">session id 已捕获,可恢复原会话继续。</p>
                  <button className="btn btn-primary" onClick={handleSpawn} disabled={spawning}>
                    {spawning ? '启动中…' : '↻ 恢复会话'}
                  </button>
                </>
              ) : (
                <>
                  <p className="tt-hint">退出前未捕获 session id,无法恢复;可启动新会话继续工作。</p>
                  <button className="btn btn-primary" onClick={handleSpawn} disabled={spawning}>
                    {spawning ? '启动中…' : '启动新会话'}
                  </button>
                </>
              )}
            </div>
          ) : (
            <div className="tt-no-session">
              <p>{stage ? `${stage} 阶段没有 CLI 会话` : '当前没有运行中的 CLI 会话'}</p>
              <button className="btn btn-primary" onClick={handleSpawn} disabled={spawning}>
                {spawning ? '启动中…' : '启动终端'}
              </button>
            </div>
          )}
        </div>
        {showHistory && (
          <div className="tt-history-pane">
            <DialogHistory storyKey={storyKey} stage={noPlanActiveStage} />
          </div>
        )}
      </div>
    </div>
  )
}
