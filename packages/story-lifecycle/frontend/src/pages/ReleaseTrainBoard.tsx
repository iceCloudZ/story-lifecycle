import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { storyApi, type Story } from '../api/client'
import Swimlane from '../components/Swimlane'
import './ReleaseTrainBoard.css'

const LIFECYCLE_STATES = ['开发', '测试', '上线', '结项']
const WIP_LIMIT = 3
const TRAINS_STORAGE_KEY = 'release_trains'

function loadCustomTrains(): string[] {
  try {
    const raw = localStorage.getItem(TRAINS_STORAGE_KEY)
    const parsed = raw ? JSON.parse(raw) : []
    return Array.isArray(parsed) ? parsed.filter((t) => typeof t === 'string' && t.trim()) : []
  } catch {
    return []
  }
}

function saveCustomTrains(trains: string[]) {
  try {
    localStorage.setItem(TRAINS_STORAGE_KEY, JSON.stringify(trains))
  } catch {
    // ignore
  }
}

export default function ReleaseTrainBoard() {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [customTrains, setCustomTrains] = useState<string[]>(() => loadCustomTrains())
  const [newTrain, setNewTrain] = useState('')

  const { data: stories, isLoading } = useQuery({
    queryKey: ['stories'],
    queryFn: storyApi.list,
    refetchInterval: 10000,
  })

  const boardStories = useMemo(() => {
    // 状态治理:只显示已上架(ready)、非测试、且引擎未完结的 story。
    // - intakeState==='ready':过了 intake 的需求才上车
    // - !isTest:过滤测试/demo 数据(后端默认也过滤,这里双保险)
    // - status 非终态:引擎已 completed/failed/aborted/archived 的不上看板
    const TERMINAL_STATUS = ['completed', 'failed', 'aborted', 'archived']
    return (stories || []).filter(
      (s) =>
        s.intakeState === 'ready' &&
        !s.isTest &&
        !(s.status && TERMINAL_STATUS.includes(s.status)),
    )
  }, [stories])

  const trainNames = useMemo(() => {
    const fromStories = new Set<string>()
    for (const s of boardStories) {
      if (s.releaseTrain) fromStories.add(s.releaseTrain)
    }
    const merged = new Set([...customTrains, ...fromStories])
    return Array.from(merged).sort((a, b) => a.localeCompare(b, 'zh-CN'))
  }, [boardStories, customTrains])

  const grouped = useMemo(() => {
    const byTrain: Record<string, Story[]> = {}
    const unassigned: Story[] = []
    for (const s of boardStories) {
      if (s.releaseTrain) {
        byTrain[s.releaseTrain] = byTrain[s.releaseTrain] || []
        byTrain[s.releaseTrain].push(s)
      } else {
        unassigned.push(s)
      }
    }
    return { byTrain, unassigned }
  }, [boardStories])

  async function handleDrop(storyKey: string, train: string | null) {
    const story = boardStories.find((s) => s.storyKey === storyKey)
    if (!story || story.releaseTrain === train) return
    try {
      await storyApi.setReleaseTrain(storyKey, train)
      qc.invalidateQueries({ queryKey: ['stories'] })
    } catch (e) {
      alert('更新班车失败: ' + (e instanceof Error ? e.message : String(e)))
    }
  }

  function handleAddTrain() {
    const name = newTrain.trim()
    if (!name) return
    if (customTrains.includes(name)) {
      setNewTrain('')
      return
    }
    const next = [...customTrains, name]
    setCustomTrains(next)
    saveCustomTrains(next)
    setNewTrain('')
  }

  function groupByState(items: Story[]) {
    const map: Record<string, Story[]> = {}
    for (const state of LIFECYCLE_STATES) map[state] = []
    for (const s of items) {
      const state = s.lifecycleState || '开发'
      if (!map[state]) map[state] = []
      map[state].push(s)
    }
    return map
  }

  function wipCount(items: Story[]) {
    return items.filter((s) => {
      const state = s.lifecycleState || '开发'
      return state === '开发' || state === '测试'
    }).length
  }

  if (isLoading) {
    return (
      <div className="release-train-board">
        <div className="board-empty">加载中...</div>
      </div>
    )
  }

  return (
    <div className="release-train-board">
      <div className="board-header">
        <h2 className="board-title">班车看板</h2>
        <div className="board-legend">
          <span className="legend-item">列 = lifecycle_state（引擎推，只读）</span>
          <span className="legend-item">泳道 = release_train（可拖）</span>
        </div>
        <div className="board-add-train">
          <input
            value={newTrain}
            onChange={(e) => setNewTrain(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleAddTrain()}
            placeholder="新建班车（如 v3.4）"
          />
          <button className="btn btn-sm" onClick={handleAddTrain} disabled={!newTrain.trim()}>
            新建班车
          </button>
        </div>
      </div>

      <div className="board-swimlanes">
        {trainNames.map((train) => {
          const items = grouped.byTrain[train] || []
          return (
            <Swimlane
              key={train}
              train={train}
              title={`📦 ${train} 班车`}
              states={LIFECYCLE_STATES}
              storiesByState={groupByState(items)}
              wipCount={wipCount(items)}
              wipLimit={WIP_LIMIT}
              onDropStory={handleDrop}
              onCardClick={(key) => navigate(`/story/${key}`)}
            />
          )
        })}

        <div className="unassigned-section">
          <div className="unassigned-header">📥 待分配</div>
          <div
            className="unassigned-pool"
            onDragOver={(e) => e.preventDefault()}
            onDrop={(e) => {
              e.preventDefault()
              const key = e.dataTransfer.getData('text/plain')
              if (key) handleDrop(key, null)
            }}
          >
            {grouped.unassigned.length === 0 ? (
              <div className="unassigned-empty">暂无待分配 Story</div>
            ) : (
              grouped.unassigned.map((s) => (
                <UnassignedCard
                  key={s.storyKey}
                  story={s}
                  onClick={() => navigate(`/story/${s.storyKey}`)}
                  onDropToUnassigned={(key) => handleDrop(key, null)}
                />
              ))
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

function UnassignedCard({
  story,
  onClick,
  onDropToUnassigned,
}: {
  story: Story
  onClick: () => void
  onDropToUnassigned: (draggedKey: string) => void
}) {
  function handleDragStart(e: React.DragEvent) {
    e.dataTransfer.setData('text/plain', story.storyKey)
    e.dataTransfer.effectAllowed = 'move'
  }

  function handleDrop(e: React.DragEvent) {
    e.preventDefault()
    const key = e.dataTransfer.getData('text/plain')
    if (key) onDropToUnassigned(key)
  }

  return (
    <div
      className="unassigned-card"
      draggable
      onDragStart={handleDragStart}
      onClick={onClick}
      onDragOver={(e) => e.preventDefault()}
      onDrop={handleDrop}
      title={story.title}
    >
      <div className="unassigned-card-key">{story.storyKey}</div>
      <div className="unassigned-card-title">{story.title || '(未命名)'}</div>
      <span className="unassigned-card-state">{story.lifecycleState || '开发'}</span>
    </div>
  )
}
