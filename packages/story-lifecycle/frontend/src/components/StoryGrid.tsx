import { useQueryClient } from '@tanstack/react-query'
import { apiAction, storyApi } from '../api/client'
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
  loading,
}: {
  stories: StorySummary[]
  emptyHint?: string
  loading?: boolean
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

  // 卡片 ⋯ 菜单:移动生命周期态(5 态全开放)。成功后刷新列表,卡片自动跳到目标 tab。
  async function handleMove(s: StorySummary, state: string) {
    try {
      await storyApi.setLifecycle(s.storyKey, state)
      qc.invalidateQueries({ queryKey: ['stories'] })
    } catch {
      alert(`移动失败:无法设置生命周期状态为 ${state}`)
    }
  }

  // 卡片 ⋯ 菜单:删除(软删,可 POST /restore 恢复)。
  async function handleDelete(s: StorySummary) {
    if (!window.confirm('确定删除?此为软删除,可从回收站恢复。')) return
    const ok = await apiAction('DELETE', `/api/story/${s.storyKey}`)
    if (ok) qc.invalidateQueries({ queryKey: ['stories'] })
  }

  if (stories.length === 0) {
    // 首屏 fetch 未回来时显示加载中,避免闪一下「暂无 Story」空态
    if (loading) {
      return <div className="empty-state"><p>加载中…</p></div>
    }
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
          onMove={(state) => handleMove(s, state)}
          onDelete={() => handleDelete(s)}
        />
      ))}
    </div>
  )
}
