import { useState, useEffect, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { storyApi, apiAction } from '../api/client'
import type { Project, WorkspaceOption } from '../api/client'
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
  const navigate = useNavigate()
  const { stories, connected } = useStoryStore()
  const [tab, setTab] = useState<'tapd' | 'story' | 'calendar' | 'project'>('tapd')
  const [intakeModal, setIntakeModal] = useState<{ story?: StorySummary } | null>(null)
  const [intakeNotice, setIntakeNotice] = useState<StartNotice | null>(null)
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

  // TAPD 全集：日历视图仍需子任务数据，保留所有 TAPD 来源
  const tapdStories = allStories.filter((s) => s.tapdType)
  // TAPD 需求列表只展示需求(story)+缺陷(bug)，排除子任务(subtask)
  const requirementStories = tapdStories.filter((s) => s.tapdType !== 'subtask')
  // 我的 Story tab: 所有已激活的 story，不区分来源（TAPD/飞书/手工创建）
  const myStories = allStories.filter((s) => s.intakeState === 'ready')

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

  async function handleStartDev(s: StorySummary) {
    setIntakeNotice(null)
    setIntakeModal({ story: s })
  }

  async function handleIntakeConfirm(input: IntakeConfirmInput) {
    let storyKey = intakeModal?.story?.storyKey || input.key

    try {
      if (!intakeModal?.story) {
        const created = await storyApi.create({
          key: input.key,
          title: input.title,
          profile: input.profile,
          workspace: input.workspace,
          autostart: false,
        })
        storyKey = created.storyKey
      }

      const r = await fetch(`/api/story/${storyKey}/start`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_ids: input.projectIds, content: input.content, branch: input.branch }),
      })
      if (!r.ok) {
        let err: Record<string, unknown>
        try {
          err = await r.json()
        } catch {
          err = {
            reasonCode: 'start_failed',
            message: `请求失败 (${r.status}): 服务器未返回详细错误`,
          }
        }
        const notice = normalizeStartNotice(err)
        setIntakeNotice(notice)
        if (notice.reasonCode === 'dingtalk_download_required') {
          for (const link of notice.dingtalkLinks) {
            window.open(link, '_blank', 'noopener,noreferrer')
          }
        }
        return
      }

      setIntakeModal(null)
      setIntakeNotice(null)
      qc.invalidateQueries({ queryKey: ['stories'] })
      navigate(`/story/${storyKey}`)
    } catch (err) {
      setIntakeNotice({
        reasonCode: 'start_failed',
        message: err instanceof Error ? err.message : '启动失败，请稍后重试',
        dingtalkLinks: [],
        questions: [],
      })
    }
  }

  return (
    <div className="dashboard">
      <div className="dashboard-header">
        <h2>Story Dashboard</h2>
        <div className="dashboard-meta">
          <span className={`ws-dot ${connected ? 'connected' : 'disconnected'}`} />
          <span>{connected ? '已连接' : '断开连接'}</span>
          <span className="story-count">
            {tab === 'project' ? `${projectCount} 个项目` : `${tab === 'tapd' ? requirementStories.length : myStories.length} 个 Story`}
          </span>
          {tab === 'project' ? (
            <button className="btn btn-primary" onClick={() => setShowProjectForm(!showProjectForm)}>
              {showProjectForm ? '取消' : '注册项目'}
            </button>
          ) : (
            <button className="btn btn-primary" onClick={() => { setIntakeNotice(null); setIntakeModal({}) }}>
              新建并开始
            </button>
          )}
        </div>
      </div>

      <div className="dashboard-tabs">
        <button className={`tab-btn ${tab === 'story' ? 'active' : ''}`} onClick={() => setTab('story')}>
          我的 Story
        </button>
        <button className={`tab-btn ${tab === 'tapd' ? 'active' : ''}`} onClick={() => setTab('tapd')}>
          TAPD 需求 {requirementStories.length > 0 && <span className="tab-count">({requirementStories.length})</span>}
        </button>
        <button className={`tab-btn ${tab === 'calendar' ? 'active' : ''}`} onClick={() => setTab('calendar')}>
          日历
        </button>
        <button className={`tab-btn ${tab === 'project' ? 'active' : ''}`} onClick={() => setTab('project')}>
          项目
        </button>
      </div>

      <div className="story-grid">
        {tab === 'tapd' && (
          requirementStories.length === 0 ? (
            <div className="empty-state">
              <p>暂无 TAPD 需求</p>
              <p className="hint">运行 <code>story sync --all</code> 从 TAPD 同步</p>
            </div>
          ) : (
            <TapdSwimlanes stories={requirementStories} onStartDev={handleStartDev} />
          )
        )}
        {tab === 'calendar' && (
          <CalendarView stories={tapdStories} />
        )}
        {tab === 'story' && (
          myStories.length === 0 ? (
            <div className="empty-state">
              <p>暂无活跃的 Story</p>
              <p className="hint">在 TAPD 需求 Tab 点击「开始开发」或使用 <code>story create KEY</code> 创建</p>
            </div>
          ) : (
            myStories.map((s) => (
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

      {intakeModal && (
        <IntakeStartModal
          story={intakeModal.story}
          notice={intakeNotice}
          onClose={() => { setIntakeModal(null); setIntakeNotice(null) }}
          onConfirm={handleIntakeConfirm}
        />
      )}
    </div>
  )
}

// ---- Intake / Start modal ----

type StartNotice = {
  reasonCode: string
  message: string
  dingtalkLinks: string[]
  questions: string[]
}

type IntakeConfirmInput = {
  key: string
  title: string
  profile: string
  workspace: string
  projectIds: number[]
  content: string
  branch: string
}

function normalizeStartNotice(err: Record<string, unknown>): StartNotice {
  return {
    reasonCode: String(err.reasonCode || 'start_failed'),
    message: String(err.message || err.detail || '无法启动'),
    dingtalkLinks: Array.isArray(err.dingtalk_links) ? err.dingtalk_links.map(String) : [],
    questions: Array.isArray(err.questions) ? err.questions.map(String) : [],
  }
}

function IntakeStartModal({ story, notice, onClose, onConfirm }: {
  story?: StorySummary
  notice: StartNotice | null
  onClose: () => void
  onConfirm: (input: IntakeConfirmInput) => void | Promise<void>
}) {
  const isNew = !story
  const [key, setKey] = useState('')
  const [title, setTitle] = useState('')
  const profile = 'minimal'
  const [workspace, setWorkspace] = useState('')
  const [workspaces, setWorkspaces] = useState<WorkspaceOption[]>([])
  const [workspaceLoading, setWorkspaceLoading] = useState(false)
  const [workspaceError, setWorkspaceError] = useState('')
  const [allProjects, setAllProjects] = useState<Project[]>([])
  const [selectedProjects, setSelectedProjects] = useState<number[]>([])
  const [paste, setPaste] = useState('')
  const [uploaded, setUploaded] = useState<{ name: string; content: string } | null>(null)
  // 最终发给后端的 PRD 正文：上传文件读出的内容，或粘贴的文本。后端会存成文件、
  // 注入文件路径给 CLI（不内联内容，避免撑爆上下文）。
  const content = uploaded ? uploaded.content : paste
  const [intakeImages, setIntakeImages] = useState<File[]>([])
  const [loading, setLoading] = useState(false)
  const [previewLoading, setPreviewLoading] = useState(false)
  const [previewStartedAt, setPreviewStartedAt] = useState<number | null>(null)
  const [localNotice, setLocalNotice] = useState<StartNotice | null>(null)
  const [branch, setBranch] = useState('')
  const activeNotice = localNotice || notice

  useEffect(() => {
    if (!isNew) return
    let alive = true
    setWorkspaceLoading(true)
    storyApi.workspaces()
      .then((data) => {
        if (!alive) return
        const items = data.workspaces || []
        setWorkspaces(items)
        setWorkspace((current) => current || items[0]?.path || '')
        setWorkspaceError('')
      })
      .catch((err) => {
        if (!alive) return
        setWorkspaceError(err instanceof Error ? err.message : '工作区加载失败')
      })
      .finally(() => {
        if (alive) setWorkspaceLoading(false)
      })
    return () => { alive = false }
  }, [isNew])

  // Load all registered projects once (for the new-story project picker).
  useEffect(() => {
    if (!isNew) return
    let alive = true
    storyApi.projects()
      .then((data) => { if (alive) setAllProjects(data.projects || []) })
      .catch(() => { /* projects optional; picker just stays empty */ })
    return () => { alive = false }
  }, [isNew])

  // Projects under the selected workspace: a registered project belongs to a
  // workspace when its repo_path lives under the workspace root. Mirrors the
  // backend's _workspace_root_for_project ancestor-walk semantics.
  const workspaceProjects = useMemo(() => {
    if (!workspace) return []
    const norm = (p: string) => p.replace(/\\/g, '/').replace(/\/+$/, '')
    const ws = norm(workspace)
    return allProjects.filter((p) => {
      const rp = p.repo_path ? norm(p.repo_path) : ''
      return rp === ws || rp.startsWith(ws + '/')
    })
  }, [allProjects, workspace])

  // Switching workspace invalidates the previous selection.
  useEffect(() => { setSelectedProjects([]) }, [workspace])

  async function handlePreview() {
    const rawId = story ? (story.sourceId || story.storyKey) : key
    const sourceId = rawId.trim().replace(/^tapd-/, '')
    if (!sourceId) return
    setPreviewLoading(true)
    setPreviewStartedAt(Date.now())
    setLocalNotice(null)
    try {
      const preview = await storyApi.previewIntake({ source_type: 'tapd', source_id: sourceId, files: intakeImages })
      if (isNew) setKey(preview.storyKey)
      if (preview.title) setTitle(preview.title)
      if (preview.branch) setBranch(preview.branch)
      if (preview.action === 'generated' && preview.markdown.trim()) {
        setUploaded(null)
        setPaste(preview.markdown)
        setLocalNotice({
          reasonCode: 'intake_prd_generated',
          message: preview.summary || '已根据来源生成 PRD 草稿，请确认后继续。',
          dingtalkLinks: [],
          questions: [],
        })
      } else if (preview.action === 'manual_download_required') {
        const links = preview.dingtalkLinks || []
        setLocalNotice({
          reasonCode: 'dingtalk_download_required',
          message: preview.summary || '请先打开外部文档并下载/复制 PRD 内容。',
          dingtalkLinks: links,
          questions: [],
        })
        for (const link of links) window.open(link, '_blank', 'noopener,noreferrer')
      } else {
        setLocalNotice({
          reasonCode: `intake_${preview.action}`,
          message: preview.summary || '读取需求后仍需补充信息。',
          dingtalkLinks: preview.dingtalkLinks || [],
          questions: preview.questions || [],
        })
      }
    } catch (err) {
      setLocalNotice({
        reasonCode: 'intake_preview_failed',
        message: err instanceof Error ? err.message : '读取需求失败',
        dingtalkLinks: [],
        questions: [],
      })
    } finally {
      setPreviewLoading(false)
      setPreviewStartedAt(null)
    }
  }

  function handleConfirm() {
    if (isNew && (!key.trim() || !workspace.trim() || !content.trim())) return
    if (isNew && selectedProjects.length === 0) return
    setLoading(true)
    Promise.resolve(onConfirm({
      key: key.trim(),
      title: title.trim(),
      profile,
      workspace: workspace.trim(),
      projectIds: selectedProjects,
      content,
      branch,
    })).finally(() => setLoading(false))
  }

  function handleFile(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0]
    if (!f) return
    const reader = new FileReader()
    reader.onload = () =>
      setUploaded({ name: f.name, content: String(reader.result || '') })
    reader.readAsText(f)
  }

  function handleImageFiles(e: React.ChangeEvent<HTMLInputElement>) {
    const selected = Array.from(e.target.files || [])
    if (!selected.length) return
    setIntakeImages((prev) => [...prev, ...selected])
  }

  function removeImage(index: number) {
    setIntakeImages((prev) => prev.filter((_, i) => i !== index))
  }

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-card" onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <span>{isNew ? '新建并开始' : '开始开发'}</span>
          <button className="modal-close" onClick={onClose}>✕</button>
        </div>
        <p className="modal-subtitle">
          {story ? story.title : '创建 Story、选择工作区，并准备 PRD 后进入规划'}
        </p>
        <div className="modal-body">
          {isNew && (
            <div className="modal-story-fields">
              <div className="story-id-field">
                <input value={key} onChange={(e) => setKey(e.target.value)} placeholder="TAPD Story ID / Story Key" />
                <button
                  type="button"
                  className="btn btn-sm"
                  disabled={!key.trim() || previewLoading}
                  onClick={handlePreview}
                >
                  {previewLoading ? '读取中...' : '读取需求'}
                </button>
              </div>
              <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="标题" />
              <label className="modal-field">
                <span>工作区 <span className="req">*</span></span>
                <select
                  value={workspace}
                  onChange={(e) => setWorkspace(e.target.value)}
                  disabled={workspaceLoading || workspaces.length === 0}
                >
                  {workspaces.length === 0 && <option value="">暂无可选工作区</option>}
                  {workspaces.map((item) => (
                    <option key={item.path} value={item.path}>
                      {item.name} · {item.projectCount} 个项目 · {item.path}
                    </option>
                  ))}
                </select>
              </label>
              {workspaceError && <p className="hint danger">{workspaceError}</p>}
              {!workspaceError && workspaces.length === 0 && !workspaceLoading && (
                <p className="hint danger">没有可选工作区，请先在“项目”页注册 monorepo 下的项目。</p>
              )}
              {workspace && (
                <div className="modal-field project-picker">
                  <span>受影响项目 <span className="req">*</span></span>
                  {workspaceProjects.length === 0 ? (
                    <p className="hint danger">该工作区下没有已注册项目，请先在「项目」页注册。</p>
                  ) : (
                    <div className="project-checkboxes">
                      {workspaceProjects.map((p) => {
                        const pid = Number(p.id)
                        const checked = selectedProjects.includes(pid)
                        return (
                          <label key={p.id} className="project-checkbox">
                            <input
                              type="checkbox"
                              checked={checked}
                              onChange={(e) => {
                                setSelectedProjects((prev) =>
                                  e.target.checked
                                    ? prev.includes(pid) ? prev : [...prev, pid]
                                    : prev.filter((x) => x !== pid)
                                )
                              }}
                            />
                            <span>{p.name}</span>
                          </label>
                        )
                      })}
                    </div>
                  )}
                  {workspaceProjects.length > 0 && selectedProjects.length === 0 && (
                    <p className="hint danger">请至少选择一个受影响的项目</p>
                  )}
                </div>
              )}
            </div>
          )}
          {!isNew && story?.sourceType === 'tapd' && (
            <button
              type="button"
              className="btn btn-sm intake-read-btn"
              disabled={previewLoading}
              onClick={handlePreview}
            >
              {previewLoading ? '读取中...' : '读取需求并生成 PRD 草稿'}
            </button>
          )}
          <p className="hint">先选择 monorepo 工作区与受影响项目，再准备 PRD。</p>
          {previewLoading && (
            <div className="intake-loading">
              <div className="intake-spinner" />
              <div>
                <div className="intake-loading-title">正在读取需求</div>
                <div className="intake-loading-copy">
                  正在拉取 TAPD 详情并调用内置 PRD generator。通常需要 10-30 秒；TAPD 或 LLM 较慢时会更久。
                  {previewStartedAt && Date.now() - previewStartedAt > 45000 && ' 已超过 45 秒，可能是 TAPD 或 LLM 响应较慢。'}
                </div>
              </div>
            </div>
          )}
          {activeNotice && (
            <div className={`intake-notice intake-${activeNotice.reasonCode}`}>
              <div>{activeNotice.message}</div>
              {activeNotice.dingtalkLinks.length > 0 && (
                <div className="intake-links">
                  {activeNotice.dingtalkLinks.map((link) => (
                    <a key={link} href={link} target="_blank" rel="noopener noreferrer">{link}</a>
                  ))}
                </div>
              )}
              {activeNotice.questions.length > 0 && (
                <ul>
                  {activeNotice.questions.map((q) => <li key={q}>{q}</li>)}
                </ul>
              )}
            </div>
          )}
          <div className="modal-images">
            <div className="modal-images-head">
              <label className="modal-prd-label">需求截图（可选）</label>
              <label className="modal-prd-upload" title="上传图片辅助理解需求">
                🖼️ 上传图片
                <input
                  type="file"
                  accept="image/*"
                  multiple
                  onChange={handleImageFiles}
                  hidden
                />
              </label>
            </div>
            {intakeImages.length > 0 && (
              <div className="modal-image-list">
                {intakeImages.map((file, idx) => (
                  <div key={`${file.name}-${idx}`} className="modal-image-item">
                    <span>🖼️ {file.name}</span>
                    <button
                      type="button"
                      className="modal-prd-clear"
                      onClick={() => removeImage(idx)}
                    >
                      移除
                    </button>
                  </div>
                ))}
              </div>
            )}
            <p className="modal-images-hint">
              若 TAPD 正文包含截图但无法自动识别，可手动上传截图，读取需求时会一并传给 AI 分析。
            </p>
          </div>
          <div className="modal-prd">
            <div className="modal-prd-head">
              <label className="modal-prd-label">
                Story 内容 / PRD {isNew && <span className="req">*</span>}
              </label>
              {!uploaded && (
                <label className="modal-prd-upload" title="上传本地 .md/.txt 文件">
                  📂 上传本地文件
                  <input
                    type="file"
                    accept=".md,.markdown,.txt,text/*"
                    onChange={handleFile}
                    hidden
                  />
                </label>
              )}
            </div>
            {uploaded ? (
              <div className="modal-prd-file">
                📄 {uploaded.name}
                <button
                  type="button"
                  className="modal-prd-clear"
                  onClick={() => setUploaded(null)}
                >
                  清除（改用粘贴）
                </button>
              </div>
            ) : (
              <textarea
                className="modal-prd-input"
                value={paste}
                onChange={(e) => setPaste(e.target.value)}
                placeholder={story
                  ? '可留空：后台会让内置 PRD generator 根据来源判断；如已下载钉钉文档，可粘贴或上传'
                  : '粘贴需求 / PRD，或用上方按钮上传本地文件；后台会保存为 PRD.md'}
                rows={6}
              />
            )}
          </div>
        </div>
        <div className="modal-footer">
          <button className="btn" onClick={onClose}>取消</button>
          <button
            className="btn btn-primary"
            disabled={(isNew && (!key.trim() || !workspace.trim() || !content.trim() || selectedProjects.length === 0)) || loading}
            onClick={handleConfirm}
          >
            {loading ? '处理中...' : '准备 PRD 并进入规划'}
          </button>
        </div>
      </div>
    </div>
  )
}

// ---- Swimlane layout for TAPD ----

const DONE_STATUSES = new Set(['resolved', 'rejected', 'closed', 'status_21'])
const LOCAL_DONE_STATUSES = new Set(['completed', 'failed', 'aborted', 'archived'])

function groupByLane(stories: StorySummary[]) {
  const today = new Date().toISOString().slice(0, 10)
  const soon = new Date(Date.now() + 3 * 86400000).toISOString().slice(0, 10)

  const lanes: { id: string; title: string; items: StorySummary[]; collapsed?: boolean }[] = [
    { id: 'candidate', title: '待确认', items: [] },
    { id: 'planning', title: '规划中', items: [] },
    { id: 'developing', title: '开发中', items: [] },
    { id: 'launch', title: '近期上线', items: [] },
    { id: 'bugs', title: '待修复缺陷', items: [] },
    { id: 'done', title: '已完成 / 已归档', items: [], collapsed: true },
    { id: 'others', title: '其他需求', items: [], collapsed: true },
  ]

  const sortByDeadline = (a: StorySummary, b: StorySummary) =>
    (a.deadline || '9').localeCompare(b.deadline || '9')
  const priOrder: Record<string, number> = { urgent: 0, high: 1, medium: 2, low: 3 }

  for (const s of stories) {
    const tp = s.tapdType || ''
    const st = s.tapdStatus || ''
    const localStatus = s.status || ''
    const intakeState = s.intakeState || ''
    const isDone = DONE_STATUSES.has(st) || LOCAL_DONE_STATUSES.has(localStatus)

    if (isDone) {
      lanes[5].items.push(s)
      continue
    }

    if (tp === 'bug') {
      lanes[4].items.push(s)
      continue
    }

    if (intakeState === 'candidate') {
      lanes[0].items.push(s)
      continue
    }

    if (localStatus === 'planning') {
      lanes[1].items.push(s)
      continue
    }

    if (['active', 'paused', 'blocked', 'waiting_subtasks'].includes(localStatus)) {
      const dl = (s.deadline || '').slice(0, 10)
      const isToday = dl === today
      const isSoon = dl >= today && dl <= soon
      if (tp === 'story' && (isToday || isSoon)) {
        lanes[3].items.push(s)
      } else {
        lanes[2].items.push(s)
      }
      continue
    }

    lanes[6].items.push(s)
  }

  lanes[0].items.sort(sortByDeadline)
  lanes[1].items.sort(sortByDeadline)
  lanes[2].items.sort(sortByDeadline)
  lanes[3].items.sort(sortByDeadline)
  lanes[4].items.sort((a, b) => (priOrder[a.priority ?? ''] ?? 9) - (priOrder[b.priority ?? ''] ?? 9))
  lanes[5].items.sort((a, b) => (b.updatedAt || '').localeCompare(a.updatedAt || ''))
  lanes[6].items.sort(sortByDeadline)

  return lanes.filter((l) => l.items.length > 0)
}

function TapdSwimlanes({ stories, onStartDev }: { stories: StorySummary[]; onStartDev: (s: StorySummary) => void }) {
  const qc = useQueryClient()
  const lanes = groupByLane(stories)
  const [linking, setLinking] = useState<string | null>(null)

  async function handleDropBug(storyKey: string, bugKey: string) {
    setLinking(`${bugKey} -> ${storyKey}`)
    try {
      await linkBugToStory(storyKey, bugKey)
      qc.invalidateQueries({ queryKey: ['stories'] })
    } catch (e) {
      alert('关联失败：' + (e as Error).message)
    } finally {
      setLinking(null)
    }
  }

  return (
    <div className="swimlanes">
      {lanes.map((lane) => (
        <Lane
          key={lane.id}
          {...lane}
          onStartDev={onStartDev}
          onDropBug={lane.id !== 'bugs' && lane.id !== 'done' ? handleDropBug : undefined}
        />
      ))}
      {linking && <div className="linking-toast">关联中 {linking}...</div>}
    </div>
  )
}

function Lane({ title, items, collapsed, onStartDev, onDropBug }: {
  title: string; items: StorySummary[]; collapsed?: boolean; onStartDev: (s: StorySummary) => void; onDropBug?: (storyKey: string, bugKey: string) => void
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
            <MiniCard
              key={s.storyKey}
              story={s}
              onStartDev={() => onStartDev(s)}
              draggable={s.tapdType === 'bug'}
              onDropBug={onDropBug ? (bugKey) => onDropBug(s.storyKey, bugKey) : undefined}
            />
          ))}
        </div>
      )}
    </div>
  )
}

async function linkBugToStory(storyKey: string, bugKey: string) {
  const r = await fetch(`/api/story/${storyKey}/bugs/${bugKey}/link`, { method: 'POST' })
  if (!r.ok) throw new Error('link failed')
  return r.json()
}

function MiniCard({ story, onStartDev, draggable, onDragStart, onDropBug }: { story: StorySummary; onStartDev: () => void; draggable?: boolean; onDragStart?: (e: React.DragEvent) => void; onDropBug?: (bugKey: string) => void }) {
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

  const [dropOver, setDropOver] = useState(false)

  return (
    <div
      className={`mini-card ${draggable ? 'mini-card-draggable' : ''} ${dropOver ? 'mini-card-drop-over' : ''}`}
      draggable={draggable}
      onDragStart={onDragStart}
      onDragOver={(e) => {
        if (!onDropBug) return
        e.preventDefault()
        setDropOver(true)
      }}
      onDragLeave={() => setDropOver(false)}
      onDrop={(e) => {
        if (!onDropBug) return
        e.preventDefault()
        setDropOver(false)
        const bugKey = e.dataTransfer.getData('text/plain')
        if (bugKey && bugKey.startsWith('tapd-bug_')) {
          onDropBug(bugKey)
        }
      }}
      onClick={() => navigate(`/story/${story.storyKey}`)}
    >
      <div className="mini-top">
        {typeInfo && <span className="badge-type" style={{ background: typeInfo.color }}>{typeInfo.label}</span>}
        <span className="mini-status">{statusCn}</span>
        {deadlineLabel && <span className={deadlineClass}>{deadlineLabel}</span>}
      </div>
      <div className="mini-title">{story.title || '(未命名)'}</div>
      <div className="mini-actions">
        {story.tapdType === 'story' && (
          <button className="btn btn-xs btn-primary" onClick={(e) => { e.stopPropagation(); onStartDev() }}>
            {story.intakeState === 'candidate' ? '确认需求' : '开始开发'}
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
  const [projects, setProjects] = useState<Project[]>([])

  function loadProjects() {
    fetch('/api/projects').then(r => r.json()).then(d => {
      setProjects(d.projects || [])
      onCountChange((d.projects || []).length)
    })
  }
  // Mount-only initial load; loadProjects is intentionally omitted from deps to
  // avoid refetch storms when the parent re-renders.
  // eslint-disable-next-line react-hooks/exhaustive-deps
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
        {projects.map((p: Project) => (
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
