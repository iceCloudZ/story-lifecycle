import { useStories } from '../../hooks/useStories'
import StoryGrid from '../../components/StoryGrid'
import './LifecyclePage.css'

/**
 * 开发中 — lifecycle_state='开发' 且已启动的 story。
 *
 * 生命周期第二段:已完成 intake/start,正在跑 design→build 阶段(或 paused 等确认)。
 */
export default function DevPage() {
  const { stories: allStories } = useStories()
  const devStories = allStories.filter(
    (s) => s.lifecycleState === '开发' && s.status !== 'idle'
  )

  return (
    <div className="lifecycle-page">
      <div className="lifecycle-header">
        <h2>开发中</h2>
        <span className="story-count">{devStories.length} 个 Story</span>
      </div>
      <StoryGrid
        stories={devStories}
        emptyHint="没有开发中的 Story。在「待启动」页点击开始开发。"
      />
    </div>
  )
}
