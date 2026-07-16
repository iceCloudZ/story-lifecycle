import { useState } from 'react'
import { useStories } from '../../hooks/useStories'
import StoryGrid from '../../components/StoryGrid'
import ReleaseTrainBoard from '../ReleaseTrainBoard'
import './LifecyclePage.css'

/**
 * 测试·上线 — lifecycle_state in ['测试','上线'] 的 story。
 *
 * 生命周期第三段:开发完成,进入 verify 验证 / 等车待发布。
 * 提供两种视图:列表(StoryGrid)和班车看板(ReleaseTrainBoard,按 release_train 泳道)。
 * 班车看板的列本来就是 lifecycle_state,是同一数据的另一种排布,故并入此页。
 */
export default function TestReleasePage() {
  const { stories: allStories } = useStories()
  const trStories = allStories.filter(
    (s) => s.lifecycleState === '测试' || s.lifecycleState === '上线'
  )
  const [view, setView] = useState<'list' | 'train'>('list')

  return (
    <div className="lifecycle-page">
      <div className="lifecycle-header">
        <h2>测试·上线</h2>
        <span className="story-count">{trStories.length} 个 Story</span>
        <div className="view-switch">
          <button
            className={`tab-btn ${view === 'list' ? 'active' : ''}`}
            onClick={() => setView('list')}
          >
            列表
          </button>
          <button
            className={`tab-btn ${view === 'train' ? 'active' : ''}`}
            onClick={() => setView('train')}
          >
            班车看板
          </button>
        </div>
      </div>
      {view === 'list' ? (
        <StoryGrid
          stories={trStories}
          emptyHint="没有测试/上线中的 Story。开发完成后会自动进入测试。"
        />
      ) : (
        <ReleaseTrainBoard />
      )}
    </div>
  )
}
