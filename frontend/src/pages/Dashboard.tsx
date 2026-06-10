import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { storyApi, apiAction } from '../api/client'
import { useStoryStore, type StorySummary } from '../store/storyStore'
import './Dashboard.css'

const STATUS_LABELS: Record<string, string> = {
  active: '运行中',
  paused: '已暂停',
  blocked: '已阻塞',
  completed: '已完成',
  failed: '已失败',
  aborted: '已终止',
  waiting_subtasks: '等待子任务',
}

const STAGES = ['design', 'implement', 'test'] as const

const CARD_ACTIONS: Record<string, { label: string; method: string; suffix: string; confirm?: string }[]> = {
  active: [
    { label: '跳过', method: 'PUT', suffix: '/skip/{stage}' },
    { label: '终止', method: 'POST', suffix: '/abort', confirm: '确定终止？' },
  ],
  paused: [{ label: '继续', method: 'PUT', suffix: '/advance' }],
  blocked: [{ label: '重试', method: 'PUT', suffix: '/advance' }],
  failed: [{ label: '删除', method: 'DELETE', suffix: '', confirm: '确定删除？' }],
  completed: [{ label: '删除', method: 'DELETE', suffix: '', confirm: '确定删除？' }],
  aborted: [{ label: '删除', method: 'DELETE', suffix: '', confirm: '确定删除？' }],
}

export default function Dashboard() {
  const { stories, connected } = useStoryStore()
  const [showCreate, setShowCreate] = useState(false)
  const qc = useQueryClient()

  const { data: fullList } = useQuery({
    queryKey: ['stories'],
    queryFn: storyApi.list,
    initialData: stories,
    refetchInterval: 10000,
  })
  const allStories = fullList ?? []

  function handleCreate(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault()
    const form = new FormData(e.currentTarget)
    storyApi.create({
      key: form.get('key') as string,
      title: (form.get('title') as string) || '',
      profile: (form.get('profile') as string) || 'minimal',
    }).then(() => {
      setShowCreate(false)
      qc.invalidateQueries({ queryKey: ['stories'] })
    })
  }

  async function handleCardAction(s: StorySummary, action: (typeof CARD_ACTIONS[string])[0]) {
    if (action.confirm && !window.confirm(action.confirm)) return
    let url = `/api/story/${s.storyKey}`
    if (action.suffix === '/skip/{stage}') {
      url += `/skip/${s.currentStage}`
    } else if (action.suffix) {
      url += action.suffix
    }
    const ok = await apiAction(action.method, url)
    if (ok) qc.invalidateQueries({ queryKey: ['stories'] })
  }

  return (
    <div className="dashboard">
      <div className="dashboard-header">
        <h2>Story Dashboard</h2>
        <div className="dashboard-meta">
          <span className={`ws-dot ${connected ? 'connected' : 'disconnected'}`} />
          <span>{connected ? '已连接' : '断开连接'}</span>
          <span className="story-count">{allStories.length} 个 Story</span>
          <button className="btn btn-primary" onClick={() => setShowCreate(!showCreate)}>
            新建 Story
          </button>
        </div>
      </div>

      {showCreate && (
        <form className="create-form" onSubmit={handleCreate}>
          <input name="key" placeholder="Story Key (必填)" required />
          <input name="title" placeholder="标题" />
          <select name="profile">
            <option value="minimal">minimal</option>
            <option value="strict">strict</option>
            <option value="demo">demo</option>
          </select>
          <button type="submit" className="btn btn-primary">创建</button>
          <button type="button" className="btn" onClick={() => setShowCreate(false)}>取消</button>
        </form>
      )}

      <div className="story-grid">
        {allStories.map((s) => (
          <StoryCard
            key={s.storyKey}
            story={s}
            onAction={(a) => handleCardAction(s, a)}
          />
        ))}
        {allStories.length === 0 && (
          <div className="empty-state">
            <p>暂无活跃的 Story</p>
            <p className="hint">使用 <code>story create KEY</code> 或点击上方按钮创建</p>
          </div>
        )}
      </div>
    </div>
  )
}

function StoryCard({
  story,
  onAction,
}: {
  story: StorySummary
  onAction: (action: (typeof CARD_ACTIONS[string])[0]) => void
}) {
  const navigate = useNavigate()
  const stageIndex = STAGES.indexOf(story.currentStage as (typeof STAGES)[number])
  const progress = stageIndex >= 0 ? ((stageIndex + 1) / STAGES.length) * 100 : 0
  const actions = CARD_ACTIONS[story.status] || []

  return (
    <div className="story-card-v2">
      <div className="card-top" onClick={() => navigate(`/story/${story.storyKey}`)}>
        <span className="card-key">{story.storyKey}</span>
        <span className={`badge badge-${story.status}`}>
          {STATUS_LABELS[story.status] || story.status}
        </span>
      </div>
      <div className="card-title" onClick={() => navigate(`/story/${story.storyKey}`)}>
        {story.title || '(未命名)'}
      </div>
      <div className="card-progress" onClick={() => navigate(`/story/${story.storyKey}`)}>
        <div className="progress-bar">
          <div className="progress-fill" style={{ width: `${progress}%` }} />
        </div>
        <span className="progress-label">
          {STAGES.map((s, i) => (
            <span key={s} className={i <= stageIndex ? 'stage-done' : 'stage-pending'}>{s}</span>
          ))}
        </span>
      </div>
      <div className="card-footer">
        {story.executionCount > 0 && (
          <span className="card-meta">重试: {story.executionCount}</span>
        )}
        {actions.length > 0 && (
          <div className="card-actions" onClick={(e) => e.stopPropagation()}>
            {actions.map((a) => (
              <button
                key={a.label}
                className={`btn btn-sm ${a.method === 'DELETE' ? 'btn-danger' : ''}`}
                onClick={() => onAction(a)}
              >
                {a.label}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
