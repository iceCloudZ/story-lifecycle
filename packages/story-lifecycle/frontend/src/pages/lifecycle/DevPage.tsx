import { useStories } from '../../hooks/useStories'
import StoryGrid from '../../components/StoryGrid'
import './LifecyclePage.css'

/**
 * 开发中 — lifecycle_state='开发' 的 story。
 *
 * TABS-LIFECYCLE-STATE: 纯 lifecycleState 判据(去掉旧 status!=='idle' 条件)。
 * 生命周期第二段:已确认规划(/plan/confirm 写入「开发」),正在跑 design→build 阶段
 * (或 paused/blocked 等恢复 — 这些是引擎 status,不影响业务 tab 归属)。
 */
export default function DevPage() {
  const { stories: allStories, isLoading } = useStories()
  const devStories = allStories.filter((s) => s.lifecycleState === '开发')

  return (
    <div className="lifecycle-page">
      <div className="lifecycle-header">
        <h2>开发中</h2>
        <span className="story-count">{devStories.length} 个 Story</span>
      </div>
      <StoryGrid
        stories={devStories}
        emptyHint="没有开发中的 Story。在「待启动」页点击开始开发。"
        loading={isLoading}
      />
    </div>
  )
}
