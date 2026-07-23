import { useState } from 'react'
import { useStories } from '../../hooks/useStories'
import StoryGrid from '../../components/StoryGrid'
import './LifecyclePage.css'

// 已结项默认只展示最近创建的若干条,余量靠「显示更多」展开 —— 结项总量可能很大
// (数百条),全量渲染会拖慢首屏且不是用户当下关心的内容。
const DONE_PAGE_SIZE = 20

/**
 * 已结项 — lifecycle_state='结项' 的 story。
 *
 * TABS-LIFECYCLE-STATE: 纯 lifecycleState 判据(去掉旧 status==='archived' 兜底)。
 * 归档端点(/archive)已同步写 lifecycle_state=结项,故无需 status 兜底。
 * 生命周期终态:TAPD closed 映射到结项,或手动归档。只读历史 + 可删除。
 */
export default function DonePage() {
  const { stories: allStories, isLoading } = useStories()
  const [expanded, setExpanded] = useState(false)

  // 按创建时间倒序(最新结项在最前);createdAt 缺失时退到列表原序(稳定排序)。
  const doneStories = allStories
    .filter((s) => s.lifecycleState === '结项')
    .sort((a, b) => (b.createdAt ?? '').localeCompare(a.createdAt ?? ''))
  const shown = expanded ? doneStories : doneStories.slice(0, DONE_PAGE_SIZE)
  const hasMore = !expanded && doneStories.length > DONE_PAGE_SIZE

  return (
    <div className="lifecycle-page">
      <div className="lifecycle-header">
        <h2>已结项</h2>
        <span className="story-count">{doneStories.length} 个 Story</span>
      </div>
      <StoryGrid
        stories={shown}
        emptyHint="还没有已结项的 Story。"
        loading={isLoading}
      />
      {hasMore && (
        <div className="lifecycle-footer">
          <button
            type="button"
            className="btn"
            onClick={() => setExpanded(true)}
          >
            显示更多(共 {doneStories.length} 个)
          </button>
        </div>
      )}
    </div>
  )
}
