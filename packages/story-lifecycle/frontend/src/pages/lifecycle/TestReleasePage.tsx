import { useStories } from '../../hooks/useStories'
import StoryGrid from '../../components/StoryGrid'
import './LifecyclePage.css'

/**
 * 测试·上线 — lifecycle_state in ['测试','上线'] 的 story。
 *
 * 生命周期第三段:开发完成,进入 verify 验证 / 等车待发布。
 * 班车看板(ReleaseTrainBoard,按 release_train 泳道)已拆为独立页面 /release-train。
 */
export default function TestReleasePage() {
  const { stories: allStories, isLoading } = useStories()
  const trStories = allStories.filter(
    (s) => s.lifecycleState === '测试' || s.lifecycleState === '上线'
  )

  return (
    <div className="lifecycle-page">
      <div className="lifecycle-header">
        <h2>测试·上线</h2>
        <span className="story-count">{trStories.length} 个 Story</span>
      </div>
      <StoryGrid
        stories={trStories}
        emptyHint="没有测试/上线中的 Story。开发完成后会自动进入测试。"
        loading={isLoading}
      />
    </div>
  )
}
