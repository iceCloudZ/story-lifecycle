import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { useStories } from '../hooks/useStories'
import type { StorySummary } from '../store/storyStore'
import { IntakeStartModal, useIntakeStart } from '../components/IntakeStartModal'
import { TAPD_STATUS, TYPE_LABELS, DONE_STATUSES, LOCAL_DONE_STATUSES } from './tapdMeta'
import './lifecycle/LifecyclePage.css'
import './TapdBoardPage.css'

/**
 * TAPD 需求 — 按泳道分组的 TAPD 需求/缺陷看板(从 Dashboard 的 TAPD 需求 tab 抽出)。
 * 「开始开发 / 确认需求」打开与 Dashboard 共用的 IntakeStartModal。
 */
export default function TapdBoardPage() {
  const { stories: allStories } = useStories()
  const { intakeModal, intakeNotice, openIntake, closeIntake, handleIntakeConfirm } = useIntakeStart()

  // TAPD 需求列表只展示需求(story)+缺陷(bug)，排除子任务(subtask)
  const requirementStories = allStories.filter((s) => s.tapdType && s.tapdType !== 'subtask')

  return (
    <div className="lifecycle-page">
      <div className="lifecycle-header">
        <h2>TAPD 需求</h2>
        <span className="story-count">{requirementStories.length} 个需求</span>
      </div>

      {requirementStories.length === 0 ? (
        <div className="empty-state">
          <p>暂无 TAPD 需求</p>
          <p className="hint">运行 <code>story sync --all</code> 从 TAPD 同步</p>
        </div>
      ) : (
        <TapdSwimlanes stories={requirementStories} onStartDev={openIntake} />
      )}

      {intakeModal && (
        <IntakeStartModal
          story={intakeModal.story}
          notice={intakeNotice}
          onClose={closeIntake}
          onConfirm={handleIntakeConfirm}
        />
      )}
    </div>
  )
}

// ---- Swimlane layout for TAPD ----

function groupByLane(stories: StorySummary[]) {
  const today = new Date().toISOString().slice(0, 10)
  const soon = new Date(Date.now() + 3 * 86400000).toISOString().slice(0, 10)

  const lanes: { id: string; title: string; items: StorySummary[]; collapsed?: boolean }[] = [
    { id: 'candidate', title: '待确认', items: [] },
    { id: 'planning', title: '规划中', items: [] },
    { id: 'developing', title: '开发中', items: [] },
    { id: 'launch', title: '近期上线', items: [] },
    { id: 'bugs', title: '待修复缺陷', items: [] },
    { id: 'done', title: '已完成 / 已归档', items: [], collapsed: true },
    { id: 'others', title: '其他需求', items: [], collapsed: true },
  ]

  const sortByDeadline = (a: StorySummary, b: StorySummary) =>
    (a.deadline || '9').localeCompare(b.deadline || '9')
  const priOrder: Record<string, number> = { urgent: 0, high: 1, medium: 2, low: 3 }

  for (const s of stories) {
    const tp = s.tapdType || ''
    const st = s.tapdStatus || ''
    const localStatus = s.status || ''
    const intakeState = s.intakeState || ''
    const isDone = DONE_STATUSES.has(st) || LOCAL_DONE_STATUSES.has(localStatus)

    if (isDone) {
      lanes[5].items.push(s)
      continue
    }

    if (tp === 'bug') {
      lanes[4].items.push(s)
      continue
    }

    if (intakeState === 'candidate') {
      lanes[0].items.push(s)
      continue
    }

    if (localStatus === 'planning') {
      lanes[1].items.push(s)
      continue
    }

    if (['active', 'paused', 'blocked', 'waiting_subtasks'].includes(localStatus)) {
      const dl = (s.deadline || '').slice(0, 10)
      const isToday = dl === today
      const isSoon = dl >= today && dl <= soon
      if (tp === 'story' && (isToday || isSoon)) {
        lanes[3].items.push(s)
      } else {
        lanes[2].items.push(s)
      }
      continue
    }

    lanes[6].items.push(s)
  }

  lanes[0].items.sort(sortByDeadline)
  lanes[1].items.sort(sortByDeadline)
  lanes[2].items.sort(sortByDeadline)
  lanes[3].items.sort(sortByDeadline)
  lanes[4].items.sort((a, b) => (priOrder[a.priority ?? ''] ?? 9) - (priOrder[b.priority ?? ''] ?? 9))
  lanes[5].items.sort((a, b) => (b.updatedAt || '').localeCompare(a.updatedAt || ''))
  lanes[6].items.sort(sortByDeadline)

  return lanes.filter((l) => l.items.length > 0)
}

function TapdSwimlanes({ stories, onStartDev }: { stories: StorySummary[]; onStartDev: (s: StorySummary) => void }) {
  const qc = useQueryClient()
  const lanes = groupByLane(stories)
  const [linking, setLinking] = useState<string | null>(null)

  async function handleDropBug(storyKey: string, bugKey: string) {
    setLinking(`${bugKey} -> ${storyKey}`)
    try {
      await linkBugToStory(storyKey, bugKey)
      qc.invalidateQueries({ queryKey: ['stories'] })
    } catch (e) {
      alert('关联失败：' + (e as Error).message)
    } finally {
      setLinking(null)
    }
  }

  return (
    <div className="swimlanes">
      {lanes.map((lane) => (
        <Lane
          key={lane.id}
          {...lane}
          onStartDev={onStartDev}
          onDropBug={lane.id !== 'bugs' && lane.id !== 'done' ? handleDropBug : undefined}
        />
      ))}
      {linking && <div className="linking-toast">关联中 {linking}...</div>}
    </div>
  )
}

function Lane({ title, items, collapsed, onStartDev, onDropBug }: {
  title: string; items: StorySummary[]; collapsed?: boolean; onStartDev: (s: StorySummary) => void; onDropBug?: (storyKey: string, bugKey: string) => void
}) {
  const [open, setOpen] = useState(!collapsed)
  return (
    <div className="swimlane">
      <div className="lane-header" onClick={() => setOpen(!open)}>
        <span className="lane-title">{title}</span>
        <span className="lane-count">{items.length}</span>
        <span className="lane-toggle">{open ? '▾' : '▸'}</span>
      </div>
      {open && (
        <div className="lane-cards">
          {items.map((s) => (
            <MiniCard
              key={s.storyKey}
              story={s}
              onStartDev={() => onStartDev(s)}
              draggable={s.tapdType === 'bug'}
              onDropBug={onDropBug ? (bugKey) => onDropBug(s.storyKey, bugKey) : undefined}
            />
          ))}
        </div>
      )}
    </div>
  )
}

async function linkBugToStory(storyKey: string, bugKey: string) {
  const r = await fetch(`/api/story/${storyKey}/bugs/${bugKey}/link`, { method: 'POST' })
  if (!r.ok) throw new Error('link failed')
  return r.json()
}

function MiniCard({ story, onStartDev, draggable, onDragStart, onDropBug }: { story: StorySummary; onStartDev: () => void; draggable?: boolean; onDragStart?: (e: React.DragEvent) => void; onDropBug?: (bugKey: string) => void }) {
  const navigate = useNavigate()
  const typeInfo = TYPE_LABELS[story.tapdType || '']
  const statusCn = TAPD_STATUS[story.tapdStatus || ''] || story.tapdStatus || ''
  const dlStr = (story.deadline || '').slice(0, 10)
  const today = new Date().toISOString().slice(0, 10)
  const isOverdue = dlStr && dlStr < today
  const isToday = dlStr === today

  let deadlineLabel = ''
  let deadlineClass = ''
  if (isOverdue) { deadlineLabel = `逾期 ${dlStr}`; deadlineClass = 'dl-overdue' }
  else if (isToday) { deadlineLabel = '今天'; deadlineClass = 'dl-today' }
  else if (dlStr) { deadlineLabel = dlStr; deadlineClass = 'dl-normal' }

  const [dropOver, setDropOver] = useState(false)

  return (
    <div
      className={`mini-card ${draggable ? 'mini-card-draggable' : ''} ${dropOver ? 'mini-card-drop-over' : ''}`}
      draggable={draggable}
      onDragStart={onDragStart}
      onDragOver={(e) => {
        if (!onDropBug) return
        e.preventDefault()
        setDropOver(true)
      }}
      onDragLeave={() => setDropOver(false)}
      onDrop={(e) => {
        if (!onDropBug) return
        e.preventDefault()
        setDropOver(false)
        const bugKey = e.dataTransfer.getData('text/plain')
        if (bugKey && bugKey.startsWith('tapd-bug_')) {
          onDropBug(bugKey)
        }
      }}
      onClick={() => navigate(`/story/${story.storyKey}`)}
    >
      <div className="mini-top">
        {typeInfo && <span className="badge-type" style={{ background: typeInfo.color }}>{typeInfo.label}</span>}
        <span className="mini-status">{statusCn}</span>
        {deadlineLabel && <span className={deadlineClass}>{deadlineLabel}</span>}
      </div>
      <div className="mini-title">{story.title || '(未命名)'}</div>
      <div className="mini-actions">
        {story.tapdType === 'story' && (
          <button className="btn btn-xs btn-primary" onClick={(e) => { e.stopPropagation(); onStartDev() }}>
            {story.intakeState === 'candidate' ? '确认需求' : '开始开发'}
          </button>
        )}
        {story.tapdUrl && (
          <a
            className="btn btn-xs tapd-link"
            href={story.tapdUrl}
            target="_blank"
            rel="noopener noreferrer"
            onClick={(e) => e.stopPropagation()}
          >
            TAPD &#x2197;
          </a>
        )}
      </div>
    </div>
  )
}
