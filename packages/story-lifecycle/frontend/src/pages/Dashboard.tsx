import { useQueryClient } from '@tanstack/react-query'
import { apiAction } from '../api/client'
import { useStoryStore, type StorySummary } from '../store/storyStore'
import { useStories } from '../hooks/useStories'
import StoryCard, { type StoryCardAction } from '../components/StoryCard'
import { IntakeStartModal, useIntakeStart } from '../components/IntakeStartModal'
import './Dashboard.css'

/**
 * 待启动(Dashboard)— lifecycle_state==='待启动' 的 Story 卡片列表。
 *
 * TABS-LIFECYCLE-STATE: 四个主 tab 按 lifecycle_state 互斥判据,与引擎 status 解耦。
 * 「待启动」= start 之后、确认规划之前(DB 默认值,未经过 /plan/confirm 推进到「开发」)。
 * TAPD 需求 / 日历 / 项目 已拆为独立页面(/tapd、/calendar、/projects)。
 */
export default function Dashboard() {
  const { connected } = useStoryStore()
  const { stories: allStories } = useStories()
  const { intakeModal, intakeNotice, openIntake, closeIntake, handleIntakeConfirm } = useIntakeStart()
  const qc = useQueryClient()

  // 待启动:lifecycle_state 为「待启动」的 story(start 后、确认规划前)
  const myStories = allStories.filter((s) => s.lifecycleState === '待启动')

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
