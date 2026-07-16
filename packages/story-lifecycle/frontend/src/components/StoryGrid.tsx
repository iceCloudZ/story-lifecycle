import { useQueryClient } from '@tanstack/react-query'
import { apiAction } from '../api/client'
import type { StorySummary } from '../store/storyStore'
import StoryCard, { type StoryCardAction } from './StoryCard'

/**
 * StoryGrid — 生命周期列表页共用的卡片网格。
 *
 * 封装 StoryCard 渲染 + 状态相关 action(跳过/继续/删除...)处理 + 空状态。
 * 4 个生命周期页(待启动/开发中/测试上线/已结项)按各自的过滤条件传 stories 进来。
 */
export default function StoryGrid({
  stories,
  emptyHint,
}: {
  stories: StorySummary[]
  emptyHint?: string
}) {
  const qc = useQueryClient()

  async function handleAction(s: StorySummary, action: StoryCardAction) {
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

  if (stories.length === 0) {
    return (
      <div className="empty-state">
        <p>暂无 Story</p>
        {emptyHint && <p className="hint">{emptyHint}</p>}
      </div>
    )
  }

  return (
    <div className="story-grid">
      {stories.map((s) => (
        <StoryCard
          key={s.storyKey}
          story={s}
          onAction={(a) => handleAction(s, a)}
        />
      ))}
    </div>
  )
}
