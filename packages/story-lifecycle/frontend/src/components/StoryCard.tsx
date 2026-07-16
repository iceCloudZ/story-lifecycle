import { useNavigate } from 'react-router-dom'
import type { StorySummary } from '../store/storyStore'

/**
 * StoryCard — 从 Dashboard 抽出的可复用 story 卡片。
 *
 * 4 个生命周期列表页 + Dashboard 共用。卡片含:key/status badge、标题、阶段进度条
 * (次要 hint)、重试计数、状态相关动作按钮(跳过/继续/删除...)。
 *
 * 注:进度条目前仍按固定 STAGES 算(历史 design/implement/test)。主进度(业务状态
 * 开发/测试/上线/结项)在详情页 OverviewTab 展示,卡片只给快速 hint。
 */

export const STATUS_LABELS: Record<string, string> = {
  active: '运行中',
  paused: '已暂停',
  blocked: '已阻塞',
  completed: '已完成',
  failed: '已失败',
  aborted: '已终止',
  waiting_subtasks: '等待子任务',
}

const STAGES = ['design', 'build', 'verify'] as const

export const CARD_ACTIONS: Record<
  string,
  { label: string; method: string; suffix: string; confirm?: string }[]
> = {
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

export interface StoryCardAction {
  label: string
  method: string
  suffix: string
  confirm?: string
}

export default function StoryCard({
  story,
  onAction,
}: {
  story: StorySummary
  onAction: (action: StoryCardAction) => void
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
            <span key={s} className={i <= stageIndex ? 'stage-done' : 'stage-pending'}>
              {s}
            </span>
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
