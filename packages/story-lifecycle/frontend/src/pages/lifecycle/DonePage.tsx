import { useStories } from '../../hooks/useStories'
import StoryGrid from '../../components/StoryGrid'
import './LifecyclePage.css'

/**
 * 已结项 — lifecycle_state='结项' 的 story。
 *
 * TABS-LIFECYCLE-STATE: 纯 lifecycleState 判据(去掉旧 status==='archived' 兜底)。
 * 归档端点(/archive)已同步写 lifecycle_state=结项,故无需 status 兜底。
 * 生命周期终态:TAPD closed 映射到结项,或手动归档。只读历史 + 可删除。
 */
export default function DonePage() {
  const { stories: allStories } = useStories()
  const doneStories = allStories.filter((s) => s.lifecycleState === '结项')

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
