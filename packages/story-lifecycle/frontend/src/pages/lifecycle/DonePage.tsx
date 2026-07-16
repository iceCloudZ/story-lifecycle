import { useStories } from '../../hooks/useStories'
import StoryGrid from '../../components/StoryGrid'
import './LifecyclePage.css'

/**
 * 已结项 — lifecycle_state='结项' 或 status='archived' 的 story。
 *
 * 生命周期终态:TAPD closed 映射到结项,或手动归档。只读历史 + 可删除。
 */
export default function DonePage() {
  const { stories: allStories } = useStories()
  const doneStories = allStories.filter(
    (s) => s.lifecycleState === '结项' || s.status === 'archived'
  )

  return (
    <div className="lifecycle-page">
      <div className="lifecycle-header">
        <h2>已结项</h2>
        <span className="story-count">{doneStories.length} 个 Story</span>
      </div>
      <StoryGrid
        stories={doneStories}
        emptyHint="还没有已结项的 Story。"
      />
    </div>
  )
}
