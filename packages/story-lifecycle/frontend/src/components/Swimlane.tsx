import { useState } from 'react'
import type { Story } from '../api/client'
import WipBadge from './WipBadge'
import './Swimlane.css'

interface SwimlaneProps {
  train: string | null
  title: string
  states: string[]
  storiesByState: Record<string, Story[]>
  wipCount: number
  wipLimit?: number
  draggable?: boolean
  onDropStory: (storyKey: string, train: string | null) => void
  onCardClick: (storyKey: string) => void
}

export default function Swimlane({
  train,
  title,
  states,
  storiesByState,
  wipCount,
  wipLimit = 3,
  draggable = true,
  onDropStory,
  onCardClick,
}: SwimlaneProps) {
  const [dropOver, setDropOver] = useState(false)

  function handleDragOver(e: React.DragEvent) {
    e.preventDefault()
    setDropOver(true)
  }

  function handleDragLeave() {
    setDropOver(false)
  }

  function handleDrop(e: React.DragEvent) {
    e.preventDefault()
    setDropOver(false)
    const storyKey = e.dataTransfer.getData('text/plain')
    if (storyKey) onDropStory(storyKey, train)
  }

  return (
    <div
      className={`swimlane-board ${dropOver ? 'swimlane-drop-over' : ''}`}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
    >
      <div className="swimlane-board-header">
        <span className="swimlane-board-title">{title}</span>
        {train !== null && <WipBadge count={wipCount} limit={wipLimit} />}
      </div>
      <div className="swimlane-columns">
        {states.map((state) => (
          <div key={state} className="swimlane-column">
            <div className="swimlane-column-header">{state}</div>
            <div className="swimlane-column-body">
              {(storiesByState[state] || []).map((s) => (
                <BoardCard
                  key={s.storyKey}
                  story={s}
                  draggable={draggable}
                  onClick={() => onCardClick(s.storyKey)}
                />
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

// 生产巡检徽标(prod-patrol Phase 2):最新轮 PASS/FAIL + 时间;登记了 items
// 但还没跑过 → 灰色"未巡检";没登记 items(null)→ 不显示,避免噪音。
function PatrolBadge({ summary }: { summary?: Story['patrolSummary'] }) {
  if (!summary || summary.itemsCount === 0) return null
  const result = summary.latestResult
  const time = summary.latestRunAt && summary.latestRunAt.length >= 16 ? summary.latestRunAt.slice(5, 16) : ''
  const cls = result === 'FAIL' ? 'board-patrol-fail' : result === 'PASS' ? 'board-patrol-pass' : 'board-patrol-none'
  const label = result === 'FAIL' ? '巡检FAIL' : result === 'PASS' ? '巡检PASS' : '未巡检'
  return (
    <span className={`board-patrol-badge ${cls}`} title={`生产巡检 · ${result ?? '未巡检'}${time ? ` · ${time}` : ''}`}>
      <span className="board-patrol-dot" />
      {label}
      {time && <span className="board-patrol-time">{time}</span>}
    </span>
  )
}

function BoardCard({
  story,
  draggable,
  onClick,
}: {
  story: Story
  draggable: boolean
  onClick: () => void
}) {
  function handleDragStart(e: React.DragEvent) {
    e.dataTransfer.setData('text/plain', story.storyKey)
    e.dataTransfer.effectAllowed = 'move'
  }

  return (
    <div
      className={`board-card ${draggable ? 'board-card-draggable' : ''}`}
      draggable={draggable}
      onDragStart={handleDragStart}
      onClick={onClick}
      title={story.title}
    >
      <div className="board-card-key">{story.storyKey}</div>
      <div className="board-card-title">{story.title || '(未命名)'}</div>
      <div className="board-card-foot">
        <span className="board-card-state">{story.lifecycleState || '开发'}</span>
        <PatrolBadge summary={story.patrolSummary} />
      </div>
    </div>
  )
}
