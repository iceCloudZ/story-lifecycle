import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import './BugsTab.css'

interface BugSummary {
  storyKey: string
  title?: string
  status?: string
  tapdStatus?: string
  priority?: string
  owner?: string
  deadline?: string
}

const statusLabel: Record<string, string> = {
  idle: '未开始',
  planning: '规划中',
  active: '开发中',
  paused: '已暂停',
  blocked: '阻塞中',
  completed: '已完成',
  failed: '失败',
  aborted: '已终止',
  archived: '已归档',
}

const priClass: Record<string, string> = {
  urgent: 'bug-pri-urgent',
  high: 'bug-pri-high',
  medium: 'bug-pri-medium',
  low: 'bug-pri-low',
}

const priLabel: Record<string, string> = {
  urgent: '紧急',
  high: '高',
  medium: '中',
  low: '低',
}

export default function BugsTab({ storyKey }: { storyKey: string }) {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [syncing, setSyncing] = useState(false)
  const [linking, setLinking] = useState<string | null>(null)
  const [copiedPrompt, setCopiedPrompt] = useState<string | null>(null)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [batchCopying, setBatchCopying] = useState(false)
  const [dragOver, setDragOver] = useState(false)
  const [showAvailable, setShowAvailable] = useState(false)

  const { data: bugs, isLoading, error } = useQuery<BugSummary[]>({
    queryKey: ['story-bugs', storyKey],
    queryFn: async () => {
      const r = await fetch(`/api/story/${storyKey}/bugs`)
      if (!r.ok) throw new Error('load bugs failed')
      return r.json()
    },
  })

  const { data: availableBugs, isLoading: loadingAvailable } = useQuery<BugSummary[]>({
    queryKey: ['available-bugs', storyKey],
    queryFn: async () => {
      const r = await fetch(`/api/story/${storyKey}/available-bugs`)
      if (!r.ok) throw new Error('load available bugs failed')
      return r.json()
    },
    enabled: showAvailable,
  })

  async function syncBugs() {
    setSyncing(true)
    try {
      const r = await fetch(`/api/story/${storyKey}/sync-related-bugs`, { method: 'POST' })
      if (!r.ok) throw new Error('sync bugs failed')
      await r.json()
      qc.invalidateQueries({ queryKey: ['story-bugs', storyKey] })
      qc.invalidateQueries({ queryKey: ['available-bugs', storyKey] })
    } catch (e) {
      alert((e as Error).message)
    } finally {
      setSyncing(false)
    }
  }

  async function linkBug(bugKey: string) {
    setLinking(bugKey)
    try {
      const r = await fetch(`/api/story/${storyKey}/bugs/${bugKey}/link`, { method: 'POST' })
      if (!r.ok) throw new Error('link bug failed')
      qc.invalidateQueries({ queryKey: ['story-bugs', storyKey] })
      qc.invalidateQueries({ queryKey: ['available-bugs', storyKey] })
    } catch (e) {
      alert((e as Error).message)
    } finally {
      setLinking(null)
    }
  }

  async function copyFixPrompt(bugKey: string) {
    try {
      const r = await fetch(`/api/story/${storyKey}/bugs/${bugKey}/fix-prompt`, { method: 'POST' })
      if (!r.ok) throw new Error('generate fix prompt failed')
      const body = await r.json()
      await navigator.clipboard.writeText(body.content || '')
      setCopiedPrompt(bugKey)
      setTimeout(() => setCopiedPrompt(''), 2000)
    } catch (e) {
      alert((e as Error).message)
    }
  }

  async function copyBatchFixPrompt() {
    const keys = Array.from(selected)
    if (keys.length === 0) return
    setBatchCopying(true)
    try {
      const r = await fetch(`/api/story/${storyKey}/bugs/fix-prompt`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ bug_keys: keys }),
      })
      if (!r.ok) throw new Error('generate batch fix prompt failed')
      const body = await r.json()
      await navigator.clipboard.writeText(body.content || '')
      setCopiedPrompt('__batch__')
      setTimeout(() => setCopiedPrompt(''), 2000)
    } catch (e) {
      alert((e as Error).message)
    } finally {
      setBatchCopying(false)
    }
  }

  function toggleSelect(bugKey: string) {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(bugKey)) next.delete(bugKey)
      else next.add(bugKey)
      return next
    })
  }

  function selectAll() {
    if (!bugs) return
    if (selected.size === bugs.length) {
      setSelected(new Set())
    } else {
      setSelected(new Set(bugs.map((b) => b.storyKey)))
    }
  }

  function handleDragStart(e: React.DragEvent, bugKey: string) {
    e.dataTransfer.setData('text/plain', bugKey)
    e.dataTransfer.effectAllowed = 'move'
  }

  function handleDrop(e: React.DragEvent) {
    e.preventDefault()
    setDragOver(false)
    const bugKey = e.dataTransfer.getData('text/plain')
    if (bugKey && bugKey.startsWith('tapd-bug_')) {
      linkBug(bugKey)
    }
  }

  const BugCard = ({ bug, draggable = false, selectable = false }: { bug: BugSummary; draggable?: boolean; selectable?: boolean }) => {
    const isSelected = selected.has(bug.storyKey)
    return (
      <div
        key={bug.storyKey}
        className={`bug-card ${draggable ? 'bug-card-draggable' : ''}`}
        draggable={draggable}
        onDragStart={draggable ? (e) => handleDragStart(e, bug.storyKey) : undefined}
        onClick={() => navigate(`/story/${bug.storyKey}?tab=overview`)}
      >
        <div className="bug-card-top">
          <span className="bug-key">{bug.storyKey}</span>
          <span className={`bug-pri ${priClass[bug.priority ?? ''] || ''}`}>
            {priLabel[bug.priority ?? ''] || bug.priority || '-'}
          </span>
        </div>
        <div className="bug-title">{bug.title || '(无标题)'}</div>
        <div className="bug-meta">
          <span>本地状态：{statusLabel[bug.status ?? ''] || bug.status}</span>
          {bug.tapdStatus && <span>TAPD：{bug.tapdStatus}</span>}
          {bug.owner && <span>负责人：{bug.owner.replace(/;$/, '')}</span>}
          {bug.deadline && <span>截止：{bug.deadline.slice(0, 10)}</span>}
        </div>
        <div className="bug-actions">
          {selectable && (
            <label className="bug-check" onClick={(e) => e.stopPropagation()}>
              <input
                type="checkbox"
                checked={isSelected}
                onChange={() => toggleSelect(bug.storyKey)}
              />
              选中
            </label>
          )}
          {!draggable && (
            <button
              className="btn btn-xs btn-primary"
              onClick={(e) => { e.stopPropagation(); copyFixPrompt(bug.storyKey) }}
            >
              {copiedPrompt === bug.storyKey ? '已复制' : '复制修复提示词'}
            </button>
          )}
          {draggable && (
            <button
              className="btn btn-xs btn-link"
              onClick={(e) => { e.stopPropagation(); linkBug(bug.storyKey) }}
              disabled={linking === bug.storyKey}
            >
              {linking === bug.storyKey ? '关联中...' : '关联到当前 Story'}
            </button>
          )}
        </div>
      </div>
    )
  }

  if (isLoading) return <div className="tab-content">加载中...</div>
  if (error) return <div className="tab-content">加载失败：{(error as Error).message}</div>

  return (
    <div className="tab-content">
      <div className="bug-toolbar">
        <h3>关联缺陷</h3>
        <div className="bug-toolbar-actions">
          {bugs && bugs.length > 0 && (
            <>
              <button className="btn" onClick={selectAll}>
                {selected.size === (bugs?.length ?? 0) ? '取消全选' : '全选'}
              </button>
              <button
                className="btn btn-primary"
                onClick={copyBatchFixPrompt}
                disabled={selected.size === 0 || batchCopying}
              >
                {batchCopying
                  ? '生成中...'
                  : copiedPrompt === '__batch__'
                    ? '已复制批量提示词'
                    : `复制选中修复提示词 (${selected.size})`}
              </button>
            </>
          )}
          <button className="btn" onClick={syncBugs} disabled={syncing}>
            {syncing ? '同步中...' : '从 TAPD 同步相关缺陷'}
          </button>
        </div>
      </div>

      <div
        className={`bug-drop-zone ${dragOver ? 'drag-over' : ''}`}
        onDragOver={(e) => { e.preventDefault(); setDragOver(true) }}
        onDragLeave={() => setDragOver(false)}
        onDrop={handleDrop}
      >
        {!bugs || bugs.length === 0 ? (
          <div className="empty-state">
            暂无关联缺陷
            <div className="hint">可以从下方"未关联缺陷"列表拖拽到此处，或点击"从 TAPD 同步"</div>
          </div>
        ) : (
          <div className="bug-list">
            {bugs.map((bug) => <BugCard key={bug.storyKey} bug={bug} selectable />)}
          </div>
        )}
      </div>

      <div className="bug-available-section">
        <button className="btn btn-link" onClick={() => setShowAvailable((v) => !v)}>
          {showAvailable ? '隐藏' : '显示'}未关联缺陷 ({availableBugs?.length ?? '...'})
        </button>
        {showAvailable && (
          <div className="bug-available-hint">
            拖拽卡片到上方"关联缺陷"区域，或点击"关联到当前 Story"按钮
          </div>
        )}
        {showAvailable && loadingAvailable && <div>加载中...</div>}
        {showAvailable && availableBugs && (
          <div className="bug-list bug-list-available">
            {availableBugs.map((bug) => <BugCard key={bug.storyKey} bug={bug} draggable />)}
          </div>
        )}
      </div>
    </div>
  )
}
