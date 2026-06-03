import { useEffect, useState } from 'react'
import './StoryDetail.css'

interface StoryDetail {
  storyKey: string
  title: string
  currentStage: string
  status: string
  profile: string
  executionCount: number
  lastError: string | null
  updatedAt: string
  parentKey: string | null
  subType: string | null
  subs: { storyKey: string; subType: string; status: string; currentStage: string }[]
}

interface Props {
  storyKey: string
}

const ACTIONS: Record<string, { label: string; method: string; path: string; confirm?: string }[]> = {
  active: [
    { label: '跳过阶段', method: 'PUT', path: '/skip/{stage}' },
    { label: '终止', method: 'POST', path: '/abort', confirm: '确定终止此 Story？' },
  ],
  paused: [
    { label: '继续执行', method: 'PUT', path: '/advance' },
    { label: '跳过阶段', method: 'PUT', path: '/skip/{stage}' },
    { label: '终止', method: 'POST', path: '/abort', confirm: '确定终止此 Story？' },
  ],
  blocked: [
    { label: '重试', method: 'PUT', path: '/advance' },
    { label: '终止', method: 'POST', path: '/abort', confirm: '确定终止此 Story？' },
  ],
  failed: [
    { label: '删除', method: 'DELETE', path: '', confirm: '确定删除？不可恢复。' },
  ],
  completed: [
    { label: '删除', method: 'DELETE', path: '', confirm: '确定删除？不可恢复。' },
  ],
  aborted: [
    { label: '删除', method: 'DELETE', path: '', confirm: '确定删除？不可恢复。' },
  ],
}

async function apiAction(method: string, url: string): Promise<boolean> {
  try {
    const r = await fetch(url, { method })
    return r.ok
  } catch {
    return false
  }
}

export default function StoryDetail({ storyKey }: Props) {
  const [detail, setDetail] = useState<StoryDetail | null>(null)

  useEffect(() => {
    let alive = true
    fetch(`/api/story/${storyKey}`)
      .then(r => r.json())
      .then(d => { if (alive) setDetail(d) })
      .catch(() => {})
    return () => { alive = false }
  }, [storyKey])

  if (!detail) return <div className="detail-loading">加载中...</div>

  const actions = ACTIONS[detail.status] || []

  async function handleAction(action: (typeof actions)[0]) {
    if (action.confirm && !window.confirm(action.confirm)) return
    let url = `/api/story/${storyKey}`
    if (action.path === '/skip/{stage}') {
      url += `/skip/${detail!.currentStage}`
    } else if (action.path) {
      url += action.path
    }
    const ok = await apiAction(action.method, url)
    if (ok) {
      const r = await fetch(`/api/story/${storyKey}`)
      if (r.ok) setDetail(await r.json())
    }
  }

  return (
    <div className="detail">
      <div className="detail-header">
        <span className="detail-key">{detail.storyKey}</span>
        <span className={`badge badge-${detail.status}`}>{detail.status}</span>
      </div>

      <div className="detail-fields">
        <div className="field"><span className="label">标题</span>{detail.title || '-'}</div>
        <div className="field"><span className="label">阶段</span>{detail.currentStage}</div>
        <div className="field"><span className="label">Profile</span>{detail.profile}</div>
        <div className="field"><span className="label">重试</span>{detail.executionCount}</div>
        <div className="field"><span className="label">更新</span>{detail.updatedAt}</div>
        {detail.parentKey && <div className="field"><span className="label">父 Story</span>{detail.parentKey}</div>}
        {detail.subType && <div className="field"><span className="label">子类型</span>{detail.subType}</div>}
      </div>

      {detail.lastError && (
        <div className="detail-error">{detail.lastError}</div>
      )}

      {detail.subs?.length > 0 && (
        <div className="detail-subs">
          <div className="section-title">子 Story</div>
          {detail.subs.map(s => (
            <div key={s.storyKey} className="sub-item">
              <span>{s.storyKey}</span>
              <span className={`badge badge-${s.status}`}>{s.status}</span>
              <span>{s.currentStage}</span>
            </div>
          ))}
        </div>
      )}

      {actions.length > 0 && (
        <div className="detail-actions">
          {actions.map(a => (
            <button key={a.label} className="action-btn" onClick={() => handleAction(a)}>
              {a.label}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
