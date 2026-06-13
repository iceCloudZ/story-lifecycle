import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { storyApi, apiAction } from '../api/client'
import { useStoryStore, type StorySummary } from '../store/storyStore'
import './Dashboard.css'

const STATUS_LABELS: Record<string, string> = {
  active: '运行中',
  paused: '已暂停',
  blocked: '已阻塞',
  completed: '已完成',
  failed: '已失败',
  aborted: '已终止',
  waiting_subtasks: '等待子任务',
}

const TAPD_STATUS: Record<string, string> = {
  status_2: '待开发',
  status_3: '开发中',
  status_4: '待测试',
  status_5: '测试中',
  status_7: '待发布',
  status_8: '待产品验收',
  status_9: '待排期',
  status_11: '待评审',
  status_17: '待规划',
  status_18: '待设计',
  status_19: '未开始',
  status_20: '进行中',
  status_21: '已完成',
  status_32: '设计中',
  status_37: '待业务验收',
  resolved: '已实现',
  closed: '已关闭',
  rejected: '已拒绝',
}

const TYPE_LABELS: Record<string, { label: string; color: string }> = {
  story: { label: '需求', color: '#2563eb' },
  bug: { label: '缺陷', color: '#ef4444' },
  subtask: { label: '子任务', color: '#7c3aed' },
}

const STAGES = ['design', 'implement', 'test'] as const

const CARD_ACTIONS: Record<string, { label: string; method: string; suffix: string; confirm?: string }[]> = {
  active: [
    { label: '跳过', method: 'PUT', suffix: '/skip/{stage}' },
    { label: '终止', method: 'POST', suffix: '/abort', confirm: '确定终止？' },
  ],
  paused: [{ label: '继续', method: 'PUT', suffix: '/advance' }],
  blocked: [{ label: '重试', method: 'PUT', suffix: '/advance' }],
  failed: [{ label: '删除', method: 'DELETE', suffix: '', confirm: '确定删除？' }],
  completed: [{ label: '删除', method: 'DELETE', suffix: '', confirm: '确定删除？' }],
  aborted: [{ label: '删除', method: 'DELETE', suffix: '', confirm: '确定删除？' }],
}

export default function Dashboard() {
  const { stories, connected } = useStoryStore()
  const [tab, setTab] = useState<'tapd' | 'story' | 'calendar' | 'project'>('tapd')
  const [showCreate, setShowCreate] = useState(false)
  const [showProjectForm, setShowProjectForm] = useState(false)
  const [projectCount, setProjectCount] = useState(0)
  const qc = useQueryClient()

  const { data: fullList } = useQuery({
    queryKey: ['stories'],
    queryFn: storyApi.list,
    initialData: stories,
    refetchInterval: 10000,
  })
  const allStories = fullList ?? []

  const tapdStories = allStories.filter((s) => s.tapdType)
  const localStories = allStories.filter((s) => !s.tapdType)

  function handleCreate(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault()
    const form = new FormData(e.currentTarget)
    storyApi.create({
      key: form.get('key') as string,
      title: (form.get('title') as string) || '',
      profile: (form.get('profile') as string) || 'minimal',
    }).then(() => {
      setShowCreate(false)
      qc.invalidateQueries({ queryKey: ['stories'] })
    })
  }

  async function handleCardAction(s: StorySummary, action: (typeof CARD_ACTIONS[string])[0]) {
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

  function handleStartDev(s: StorySummary) {
    // Activate the TAPD story directly instead of creating a duplicate
    fetch(`/api/story/${s.storyKey}/start`, { method: 'POST' })
      .then(async (r) => {
        if (!r.ok) {
          const err = await r.json()
          alert(`无法启动: ${err.message || err.reasonCode || '未知错误'}\n请先为 Story 绑定项目。`)
          return
        }
        setTab('story')
        qc.invalidateQueries({ queryKey: ['stories'] })
      })
  }

  return (
    <div className="dashboard">
      <div className="dashboard-header">
        <h2>Story Dashboard</h2>
        <div className="dashboard-meta">
          <span className={`ws-dot ${connected ? 'connected' : 'disconnected'}`} />
          <span>{connected ? '已连接' : '断开连接'}</span>
          <span className="story-count">
            {tab === 'project' ? `${projectCount} 个项目` : `${tab === 'tapd' ? tapdStories.length : localStories.length} 个 Story`}
          </span>
          {tab === 'project' ? (
            <button className="btn btn-primary" onClick={() => setShowProjectForm(!showProjectForm)}>
              {showProjectForm ? '取消' : '注册项目'}
            </button>
          ) : (
            <button className="btn btn-primary" onClick={() => setShowCreate(!showCreate)}>
              新建 Story
            </button>
          )}
        </div>
      </div>

      <div className="dashboard-tabs">
        <button className={`tab-btn ${tab === 'story' ? 'active' : ''}`} onClick={() => setTab('story')}>
          我的 Story
        </button>
        <button className={`tab-btn ${tab === 'tapd' ? 'active' : ''}`} onClick={() => setTab('tapd')}>
          TAPD 需求 {tapdStories.length > 0 && <span className="tab-count">({tapdStories.length})</span>}
        </button>
        <button className={`tab-btn ${tab === 'calendar' ? 'active' : ''}`} onClick={() => setTab('calendar')}>
          日历
        </button>
        <button className={`tab-btn ${tab === 'project' ? 'active' : ''}`} onClick={() => setTab('project')}>
          项目
        </button>
      </div>

      {showCreate && (
        <form className="create-form" onSubmit={handleCreate}>
          <input name="key" placeholder="Story Key (必填)" required />
          <input name="title" placeholder="标题" />
          <select name="profile">
            <option value="minimal">minimal</option>
            <option value="strict">strict</option>
            <option value="demo">demo</option>
          </select>
          <button type="submit" className="btn btn-primary">创建</button>
          <button type="button" className="btn" onClick={() => setShowCreate(false)}>取消</button>
        </form>
      )}

      <div className="story-grid">
        {tab === 'tapd' && (
          tapdStories.length === 0 ? (
            <div className="empty-state">
              <p>暂无 TAPD 需求</p>
              <p className="hint">运行 <code>story sync --all</code> 从 TAPD 同步</p>
            </div>
          ) : (
            <TapdSwimlanes stories={tapdStories} onStartDev={handleStartDev} />
          )
        )}
        {tab === 'calendar' && (
          <CalendarView stories={tapdStories} />
        )}
        {tab === 'story' && (
          localStories.length === 0 ? (
            <div className="empty-state">
              <p>暂无活跃的 Story</p>
              <p className="hint">在 TAPD 需求 Tab 点击「开始开发」或使用 <code>story create KEY</code> 创建</p>
            </div>
          ) : (
            localStories.map((s) => (
              <StoryCard key={s.storyKey} story={s} onAction={(a) => handleCardAction(s, a)} />
            ))
          )
        )}
        {tab === 'project' && (
          <ProjectPanel
            showForm={showProjectForm}
            setShowForm={setShowProjectForm}
            onCountChange={setProjectCount}
            onRefresh={() => qc.invalidateQueries({ queryKey: ['stories'] })}
          />
        )}
      </div>
    </div>
  )
}

// ---- Swimlane layout for TAPD ----

const DONE_STATUSES = new Set(['resolved', 'rejected', 'closed', 'status_21'])

function groupByLane(stories: StorySummary[]) {
  const today = new Date().toISOString().slice(0, 10)
  const soon = new Date(Date.now() + 3 * 86400000).toISOString().slice(0, 10)

  const lanes: { id: string; title: string; items: StorySummary[]; collapsed?: boolean }[] = [
    { id: 'my-tasks', title: '我的开发任务', items: [] },
    { id: 'testing', title: '测试任务', items: [] },
    { id: 'launch', title: '近期上线', items: [] },
    { id: 'bugs', title: '待修复缺陷', items: [] },
    { id: 'others', title: '其他需求', items: [], collapsed: true },
  ]

  for (const s of stories) {
    const tp = s.tapdType || ''
    const st = s.tapdStatus || ''
    const title = s.title || ''
    const isDone = DONE_STATUSES.has(st)
    if (isDone) continue

    const dl = (s.deadline || '').slice(0, 10)
    const isToday = dl === today
    const isSoon = dl >= today && dl <= soon

    if (tp === 'subtask' && (s.owner || '').includes('赵子豪')) {
      lanes[0].items.push(s)
    } else if (tp === 'subtask' && title.includes('测试')) {
      lanes[1].items.push(s)
    } else if (tp === 'bug') {
      lanes[3].items.push(s)
    } else if (tp === 'story' && (isToday || isSoon)) {
      lanes[2].items.push(s)
    } else {
      lanes[4].items.push(s)
    }
  }

  const sortByDeadline = (a: StorySummary, b: StorySummary) =>
    (a.deadline || '9').localeCompare(b.deadline || '9')
  lanes[0].items.sort(sortByDeadline)
  lanes[1].items.sort(sortByDeadline)
  const priOrder: Record<string, number> = { urgent: 0, high: 1, medium: 2, low: 3 }
  lanes[3].items.sort((a, b) => (priOrder[a.priority ?? ''] ?? 9) - (priOrder[b.priority ?? ''] ?? 9))

  return lanes.filter((l) => l.items.length > 0)
}

function TapdSwimlanes({ stories, onStartDev }: { stories: StorySummary[]; onStartDev: (s: StorySummary) => void }) {
  const lanes = groupByLane(stories)
  return (
    <div className="swimlanes">
      {lanes.map((lane) => (
        <Lane key={lane.id} {...lane} onStartDev={onStartDev} />
      ))}
    </div>
  )
}

function Lane({ title, items, collapsed, onStartDev }: {
  title: string; items: StorySummary[]; collapsed?: boolean; onStartDev: (s: StorySummary) => void
}) {
  const [open, setOpen] = useState(!collapsed)
  return (
    <div className="swimlane">
      <div className="lane-header" onClick={() => setOpen(!open)}>
        <span className="lane-title">{title}</span>
        <span className="lane-count">{items.length}</span>
        <span className="lane-toggle">{open ? '▾' : '▸'}</span>
      </div>
      {open && (
        <div className="lane-cards">
          {items.map((s) => (
            <MiniCard key={s.storyKey} story={s} onStartDev={() => onStartDev(s)} />
          ))}
        </div>
      )}
    </div>
  )
}

function MiniCard({ story, onStartDev }: { story: StorySummary; onStartDev: () => void }) {
  const navigate = useNavigate()
  const typeInfo = TYPE_LABELS[story.tapdType || '']
  const statusCn = TAPD_STATUS[story.tapdStatus || ''] || story.tapdStatus || ''
  const dlStr = (story.deadline || '').slice(0, 10)
  const today = new Date().toISOString().slice(0, 10)
  const isOverdue = dlStr && dlStr < today
  const isToday = dlStr === today

  let deadlineLabel = ''
  let deadlineClass = ''
  if (isOverdue) { deadlineLabel = `逾期 ${dlStr}`; deadlineClass = 'dl-overdue' }
  else if (isToday) { deadlineLabel = '今天'; deadlineClass = 'dl-today' }
  else if (dlStr) { deadlineLabel = dlStr; deadlineClass = 'dl-normal' }

  return (
    <div className="mini-card" onClick={() => navigate(`/story/${story.storyKey}`)}>
      <div className="mini-top">
        {typeInfo && <span className="badge-type" style={{ background: typeInfo.color }}>{typeInfo.label}</span>}
        <span className="mini-status">{statusCn}</span>
        {deadlineLabel && <span className={deadlineClass}>{deadlineLabel}</span>}
      </div>
      <div className="mini-title">{story.title || '(未命名)'}</div>
      <div className="mini-actions">
        {story.tapdType === 'story' && (
          <button className="btn btn-xs btn-primary" onClick={(e) => { e.stopPropagation(); onStartDev() }}>
            开始开发
          </button>
        )}
        {story.tapdUrl && (
          <a
            className="btn btn-xs tapd-link"
            href={story.tapdUrl}
            target="_blank"
            rel="noopener noreferrer"
            onClick={(e) => e.stopPropagation()}
          >
            TAPD &#x2197;
          </a>
        )}
      </div>
    </div>
  )
}

// ---- Calendar view ----

function CalendarView({ stories }: { stories: StorySummary[] }) {
  const now = new Date()
  const year = now.getFullYear()
  const month = now.getMonth()
  const today = now.toISOString().slice(0, 10)

  const byDate: Record<string, StorySummary[]> = {}
  for (const s of stories) {
    if (s.tapdType !== 'subtask') continue
    const st = s.tapdStatus || ''
    if (DONE_STATUSES.has(st)) continue
    const dl = (s.deadline || '').slice(0, 10)
    if (!dl) continue
    if (!byDate[dl]) byDate[dl] = []
    byDate[dl].push(s)
  }

  const firstDay = new Date(year, month, 1)
  const lastDay = new Date(year, month + 1, 0)
  const startPad = firstDay.getDay()
  const days: string[] = []
  for (let i = 0; i < startPad; i++) days.push('')
  for (let d = 1; d <= lastDay.getDate(); d++) {
    const ds = `${year}-${String(month + 1).padStart(2, '0')}-${String(d).padStart(2, '0')}`
    days.push(ds)
  }

  const dayNames = ['日', '一', '二', '三', '四', '五', '六']

  return (
    <div className="calendar-view">
      <h3 className="cal-title">{year}年{month + 1}月</h3>
      <div className="cal-grid">
        {dayNames.map((n) => (
          <div key={n} className="cal-day-header">{n}</div>
        ))}
        {days.map((ds, i) => {
          const items = ds ? (byDate[ds] || []) : []
          const isToday = ds === today
          const d = ds ? parseInt(ds.slice(8)) : 0
          return (
            <div key={ds || `empty-${i}`} className={`cal-day ${isToday ? 'cal-today' : ''} ${ds ? '' : 'cal-empty'}`}>
              {ds && <div className="cal-date">{d}</div>}
              {items.slice(0, 6).map((s) => {
                const typeInfo = TYPE_LABELS[s.tapdType || '']
                return (
                  <div key={s.storyKey} className="cal-task" title={s.title}>
                    <span className="cal-task-dot" style={{ background: typeInfo?.color || '#7c3aed' }} />
                    <span className="cal-task-text">{(s.title || '').slice(0, 16)}</span>
                    {s.tapdUrl && (
                      <a className="cal-task-link" href={s.tapdUrl} target="_blank" rel="noopener noreferrer"
                        onClick={(e) => e.stopPropagation()} title="在 TAPD 中查看">&#x2197;</a>
                    )}
                  </div>
                )
              })}
              {items.length > 6 && <div className="cal-more">+{items.length - 6}</div>}
            </div>
          )
        })}
      </div>
    </div>
  )
}

// ---- Local story card ----

function StoryCard({ story, onAction }: {
  story: StorySummary
  onAction: (action: (typeof CARD_ACTIONS[string])[0]) => void
}) {
  const navigate = useNavigate()
  const stageIndex = STAGES.indexOf(story.currentStage as (typeof STAGES)[number])
  const progress = stageIndex >= 0 ? ((stageIndex + 1) / STAGES.length) * 100 : 0
  const actions = CARD_ACTIONS[story.status] || []

  return (
    <div className="story-card-v2">
      <div className="card-top" onClick={() => navigate(`/story/${story.storyKey}`)}>
        <span className="card-key">{story.storyKey}</span>
        <span className={`badge badge-${story.status}`}>
          {STATUS_LABELS[story.status] || story.status}
        </span>
      </div>
      <div className="card-title" onClick={() => navigate(`/story/${story.storyKey}`)}>
        {story.title || '(未命名)'}
      </div>
      <div className="card-progress" onClick={() => navigate(`/story/${story.storyKey}`)}>
        <div className="progress-bar">
          <div className="progress-fill" style={{ width: `${progress}%` }} />
        </div>
        <span className="progress-label">
          {STAGES.map((s, i) => (
            <span key={s} className={i <= stageIndex ? 'stage-done' : 'stage-pending'}>{s}</span>
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
              <button key={a.label} className={`btn btn-sm ${a.method === 'DELETE' ? 'btn-danger' : ''}`}
                onClick={() => onAction(a)}>{a.label}</button>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

// ---- Project management panel ----

function ProjectPanel({ showForm, setShowForm, onCountChange, onRefresh }: {
  showForm: boolean
  setShowForm: (v: boolean) => void
  onCountChange: (n: number) => void
  onRefresh: () => void
}) {
  const [projects, setProjects] = useState<any[]>([])

  function loadProjects() {
    fetch('/api/projects').then(r => r.json()).then(d => {
      setProjects(d.projects || [])
      onCountChange((d.projects || []).length)
    })
  }
  useEffect(() => { loadProjects() }, [])

  function handleRegister(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault()
    const form = new FormData(e.currentTarget)
    fetch('/api/projects', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: form.get('name'),
        repo_path: form.get('repo_path'),
        default_branch: form.get('default_branch') || 'main',
      }),
    }).then(r => {
      if (r.ok) { loadProjects(); setShowForm(false); onRefresh() }
      else r.json().then(err => alert('注册失败: ' + (err.detail || '未知错误')))
    })
  }

  if (projects.length === 0 && !showForm) {
    return (
      <div className="empty-state">
        <p>暂无注册项目</p>
        <p className="hint">注册项目后，TAPD Story 点击「开始开发」会自动绑定</p>
        <button className="btn btn-primary" style={{ marginTop: 12 }} onClick={() => setShowForm(true)}>
          注册第一个项目
        </button>
      </div>
    )
  }

  return (
    <div>
      {showForm && (
        <form className="create-form" onSubmit={handleRegister}>
          <input name="name" placeholder="项目名称 (如 hc-order)" required />
          <input name="repo_path" placeholder="仓库路径 (如 D:/code/my-project)" required />
          <input name="default_branch" placeholder="默认分支" defaultValue="main" />
          <button type="submit" className="btn btn-primary">保存</button>
          <button type="button" className="btn" onClick={() => setShowForm(false)}>取消</button>
        </form>
      )}

      <div className="story-grid">
        {projects.map((p: any) => (
          <div key={p.id} className="story-card-v2 project-card">
            <div className="card-top">
              <span className="card-key">{p.name}</span>
              <span className={`badge-type ${p.availability === 'available' ? 'badge-ok' : 'badge-warn'}`}
                style={{ fontSize: 10, padding: '2px 6px', borderRadius: 3 }}>
                {p.availability || 'unknown'}
              </span>
            </div>
            <p className="card-meta" style={{ wordBreak: 'break-all', marginBottom: 4 }}>
              {p.repo_path}
            </p>
            <p className="card-meta">默认分支: {p.default_branch}</p>
          </div>
        ))}
      </div>
    </div>
  )
}
