import type { StorySummary } from '../store/storyStore'
import './StoryList.css'

const STATUS_LABELS: Record<string, string> = {
  active: '运行中',
  paused: '已暂停',
  blocked: '已阻塞',
  completed: '已完成',
  failed: '已失败',
  aborted: '已终止',
  waiting_subtasks: '等待子任务',
}

interface Props {
  stories: StorySummary[]
  selectedKey: string | null
  onSelect: (key: string) => void
}

export default function StoryList({ stories, selectedKey, onSelect }: Props) {
  if (!stories.length) {
    return (
      <div className="story-list-empty">
        <div className="empty-icon">&#9889;</div>
        <p>暂无活跃的 Story</p>
        <p className="hint">使用 <code>story create KEY</code> 创建</p>
      </div>
    )
  }

  return (
    <div className="story-list">
      {stories.map(s => (
        <div
          key={s.storyKey}
          className={`story-card ${s.storyKey === selectedKey ? 'selected' : ''}`}
          onClick={() => onSelect(s.storyKey)}
        >
          <div className="card-header">
            <span className="card-key">{s.storyKey}</span>
            <span className={`badge badge-${s.status}`}>
              {STATUS_LABELS[s.status] || s.status}
            </span>
          </div>
          <div className="card-title">{s.title || '(未命名)'}</div>
          <div className="card-meta">
            <span>{s.currentStage}</span>
            {s.executionCount > 0 && <span>retries: {s.executionCount}</span>}
          </div>
        </div>
      ))}
    </div>
  )
}
