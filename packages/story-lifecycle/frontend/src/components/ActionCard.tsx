import { useState } from 'react'
import type { AgentAction } from '../api/client'

const ADAPTER_ICON: Record<string, string> = {
  claude: '🟠',
  codex: '🟢',
  kimi: '🔵',
}

const ADAPTERS = ['claude', 'codex', 'kimi']

interface StagePrompt { stage: string; path: string; content: string }
interface PromptsResponse { story_key: string; prompts: StagePrompt[] }

interface Props {
  action: AgentAction
  index: number
  storyKey: string
  // 阶段已完成(planData.stages[].done):不再显示「执行」。
  done?: boolean
  editable?: boolean
  onAdapterChange?: (index: number, adapter: string) => void
}

export default function ActionCard({ action, index, storyKey, done, editable, onAdapterChange }: Props) {
  const [spawning, setSpawning] = useState(false)
  const [spawned, setSpawned] = useState(false)
  const [copied, setCopied] = useState(false)

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

  // 全自动:与 TerminalTab「+ 新建」同一 endpoint/请求形状;adapter 留空,
  // 后端 resolve_stage_adapter 拿 plan UI 选的 adapter。spawn 针对 story 当前 stage。
  async function handleRun() {
    setSpawning(true)
    try {
      const r = await fetch(`/api/story/${storyKey}/sessions/spawn`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ adapter: '', model: '' }),
      })
      if (r.ok) {
        setSpawned(true)
      } else {
        alert(`启动失败: ${(await r.json()).detail || '未知错误'}`)
      }
    } catch {
      alert('启动失败: 网络错误')
    } finally {
      setSpawning(false)
    }
  }

  // 半自动:复制该 stage 的组装提示词(prompt 还没生成时回落到上下文资料包)。
  async function handleCopyPrompt() {
    let text = ''
    try {
      const r = await fetch(`/api/story/${storyKey}/prompts`)
      if (r.ok) {
        const data: PromptsResponse = await r.json()
        text = (data.prompts || []).find((p) => p.stage === action.stage)?.content || ''
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

  return (
    <div className="action-card action-launch">
      <div className="ac-header">
        <span className="ac-index">#{index + 1}</span>
        <span className="ac-icon">{ADAPTER_ICON[action.adapter ?? 'claude'] ?? '🔧'}</span>
        <span className="ac-stage">{action.stage}</span>
        {done && <span className="ac-badge ac-done-badge">已完成</span>}
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
      <div className="ac-actions">
        {!done && (
          <button className="btn btn-sm btn-primary" disabled={spawning} onClick={handleRun}>
            {spawning ? '启动中…' : '▶ 执行'}
          </button>
        )}
        <button className="btn btn-sm" onClick={handleCopyPrompt}>
          {copied ? '已复制' : '复制提示词'}
        </button>
        {spawned && <span className="ac-spawned-hint">已在终端启动，可切到终端查看</span>}
      </div>
    </div>
  )
}
