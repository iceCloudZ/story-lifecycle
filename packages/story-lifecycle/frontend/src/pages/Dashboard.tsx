import { useQueryClient } from '@tanstack/react-query'
import { apiAction } from '../api/client'
import { useStoryStore, type StorySummary } from '../store/storyStore'
import { useStories } from '../hooks/useStories'
import StoryCard, { type StoryCardAction } from '../components/StoryCard'
import { IntakeStartModal, useIntakeStart } from '../components/IntakeStartModal'
import './Dashboard.css'

/**
 * 待启动(Dashboard)— 已激活(intakeState==='ready')等待开始/进行中的 Story 卡片列表。
 * TAPD 需求 / 日历 / 项目 已拆为独立页面(/tapd、/calendar、/projects)。
 */
export default function Dashboard() {
  const { connected } = useStoryStore()
  const { stories: allStories } = useStories()
  const { intakeModal, intakeNotice, openIntake, closeIntake, handleIntakeConfirm } = useIntakeStart()
  const qc = useQueryClient()

  // 我的 Story: 所有已激活的 story，不区分来源（TAPD/飞书/手工创建）
  const myStories = allStories.filter((s) => s.intakeState === 'ready')

  async function handleCardAction(s: StorySummary, action: StoryCardAction) {
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

  return (
    <div className="dashboard">
      <div className="dashboard-header">
        <h2>待启动</h2>
        <div className="dashboard-meta">
          <span className={`ws-dot ${connected ? 'connected' : 'disconnected'}`} />
          <span>{connected ? '已连接' : '断开连接'}</span>
          <span className="story-count">{myStories.length} 个 Story</span>
          <button className="btn btn-primary" onClick={() => openIntake()}>
            新建并开始
          </button>
        </div>
      </div>

      <div className="story-grid">
        {myStories.length === 0 ? (
          <div className="empty-state">
            <p>暂无活跃的 Story</p>
            <p className="hint">在「TAPD 需求」页点击「开始开发」或使用 <code>story create KEY</code> 创建</p>
          </div>
        ) : (
          myStories.map((s) => (
            <StoryCard key={s.storyKey} story={s} onAction={(a) => handleCardAction(s, a)} />
          ))
        )}
      </div>

      {intakeModal && (
        <IntakeStartModal
          story={intakeModal.story}
          notice={intakeNotice}
          onClose={closeIntake}
          onConfirm={handleIntakeConfirm}
        />
      )}
    </div>
  )
}
