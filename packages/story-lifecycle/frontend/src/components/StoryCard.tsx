import { useNavigate } from 'react-router-dom'
import type { StorySummary } from '../store/storyStore'
import { TYPE_LABELS } from '../pages/tapdMeta'
import CardOverflowMenu from './CardOverflowMenu'

/**
 * StoryCard — 从 Dashboard 抽出的可复用 story 卡片。
 *
 * 4 个生命周期列表页 + Dashboard 共用。卡片含:标题/引擎 status badge、阶段进度条
 * (次要 hint)、重试计数、状态相关动作按钮(跳过/继续/重试/删除...)。
 *
 * STATUS-CQRS-REFACTOR: badge 按 4 核心态(active/paused/completed/failed)展示,
 * 旧值(implementing/blocked/waiting_subtasks/aborted)经 normalizeStatus 归一。
 * 卡片所在 tab 由 lifecycleState 决定(业务状态),badge 显示 status(引擎执行态)。
 */

/**
 * 旧 status 值 → 4 核心态归一(与后端 normalize_status 对称)。
 * 老数据可能还存旧值,展示时归一。
 */
function normalizeStatus(s: string | undefined | null): string {
  if (!s) return ''
  return (
    {
      implementing: 'active',
      blocked: 'paused',
      waiting_subtasks: 'paused',
      aborted: 'failed',
    }[s] ?? s
  )
}

/** 4 核心态的展示标签(业界 CI 三分类:运行中/等待/终态)。 */
export const STATUS_LABELS: Record<string, string> = {
  active: '运行中',
  paused: '等待中',
  completed: '已完成',
  failed: '异常',
}

const STAGES = ['design', 'build', 'verify'] as const

/**
 * 卡片动作按钮(按归一后的 4 态驱动)。
 * - active: 跳过/终止(引擎在跑)
 * - paused: 继续(含原 blocked/waiting_subtasks 合并)
 * - failed: 重试(加重试,删除降为次要 — 失败不等于废弃)
 * - completed: 无按钮(进详情页确认下一阶段,不是废弃)
 */
export const CARD_ACTIONS: Record<
  string,
  { label: string; method: string; suffix: string; confirm?: string }[]
> = {
  active: [
    { label: '跳过', method: 'PUT', suffix: '/skip/{stage}' },
    { label: '终止', method: 'POST', suffix: '/abort', confirm: '确定终止？' },
  ],
  paused: [{ label: '继续', method: 'PUT', suffix: '/advance' }],
  failed: [
    { label: '重试', method: 'PUT', suffix: '/advance' },
    { label: '删除', method: 'DELETE', suffix: '', confirm: '确定删除？' },
  ],
}

export interface StoryCardAction {
  label: string
  method: string
  suffix: string
  confirm?: string
}

export default function StoryCard({
  story,
  onAction,
  onMove,
  onDelete,
}: {
  story: StorySummary
  onAction: (action: StoryCardAction) => void
  onMove?: (state: string) => void
  onDelete?: () => void
}) {
  const navigate = useNavigate()
  const stageIndex = STAGES.indexOf(story.currentStage as (typeof STAGES)[number])
  const progress = stageIndex >= 0 ? ((stageIndex + 1) / STAGES.length) * 100 : 0
  const normalizedStatus = normalizeStatus(story.status)
  const actions = CARD_ACTIONS[normalizedStatus] || []
  // 类型 badge(需求/缺陷/子任务) — 卡片左侧紧贴标题,配色复用 TYPE_LABELS。
  // tapdType 缺失(手工建的本地 story)时不显示,避免占位。
  const typeInfo = TYPE_LABELS[story.tapdType || '']

  return (
    <div className="story-card-v2">
      <div className="card-top" onClick={() => navigate(`/story/${story.storyKey}`)}>
        <div className="card-title-cluster">
          {typeInfo && (
            <span
              className="badge-type card-type-badge"
              style={{ background: typeInfo.color }}
            >
              {typeInfo.label}
            </span>
          )}
          <span className="card-title">{story.title || '(未命名)'}</span>
        </div>
        <div className="card-top-right">
          <span className={`badge badge-${normalizedStatus}`}>
            {STATUS_LABELS[normalizedStatus] || normalizedStatus}
          </span>
          {(onMove || onDelete) && (
            <CardOverflowMenu
              currentLifecycle={story.lifecycleState}
              onMove={(state) => onMove?.(state)}
              onDelete={() => onDelete?.()}
            />
          )}
        </div>
      </div>
      <div className="card-progress" onClick={() => navigate(`/story/${story.storyKey}`)}>
        <div className="progress-bar">
          <div className="progress-fill" style={{ width: `${progress}%` }} />
        </div>
        <span className="progress-label">
          {STAGES.map((s, i) => (
            <span key={s} className={i <= stageIndex ? 'stage-done' : 'stage-pending'}>
              {s}
            </span>
          ))}
        </span>
      </div>
      <div className="card-footer">
        {story.executionCount > 0 && (
          <span className="card-meta">重试: {story.executionCount}</span>
        )}
        {actions.length > 0 && (
          <div className="card-actions" onClick={(e) => e.stopPropagation()}>
            {actions.map((a) => (
              <button
                key={a.label}
                className={`btn btn-sm ${a.method === 'DELETE' ? 'btn-danger' : ''}`}
                onClick={() => onAction(a)}
              >
                {a.label}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
