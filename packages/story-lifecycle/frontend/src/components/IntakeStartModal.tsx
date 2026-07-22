import { useState, useEffect, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { storyApi } from '../api/client'
import type { Project, WorkspaceOption, ProfileOption } from '../api/client'
import type { StorySummary } from '../store/storyStore'
import './IntakeStartModal.css'

// ---- Intake / Start modal ----
// 从 Dashboard 抽出:Dashboard(待启动)与 TapdBoardPage 都通过 useIntakeStart 打开它。

export type StartNotice = {
  reasonCode: string
  message: string
  dingtalkLinks: string[]
  questions: string[]
}

export type IntakeConfirmInput = {
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

/**
 * 管理 IntakeStartModal 的开关与「创建 + start」确认流程。
 * openIntake()          — 新建并开始
 * openIntake(story)     — 对已有 story 开始开发
 */
export function useIntakeStart() {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [intakeModal, setIntakeModal] = useState<{ story?: StorySummary } | null>(null)
  const [intakeNotice, setIntakeNotice] = useState<StartNotice | null>(null)

  function openIntake(story?: StorySummary) {
    setIntakeNotice(null)
    setIntakeModal({ story })
  }

  function closeIntake() {
    setIntakeModal(null)
    setIntakeNotice(null)
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

  return { intakeModal, intakeNotice, openIntake, closeIntake, handleIntakeConfirm }
}

export function IntakeStartModal({ story, notice, onClose, onConfirm }: {
  story?: StorySummary
  notice: StartNotice | null
  onClose: () => void
  onConfirm: (input: IntakeConfirmInput) => void | Promise<void>
}) {
  const isNew = !story
  const [key, setKey] = useState('')
  const [title, setTitle] = useState('')
  const [profile, setProfile] = useState('minimal')
  const [profiles, setProfiles] = useState<ProfileOption[]>([])
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

  // Load available profiles once (for the new-story profile picker).
  useEffect(() => {
    if (!isNew) return
    let alive = true
    storyApi.profiles()
      .then((data) => { if (alive) setProfiles(data.profiles || []) })
      .catch(() => { /* profiles optional; picker falls back to minimal */ })
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
                <span>流程 Profile</span>
                <select value={profile} onChange={(e) => setProfile(e.target.value)}>
                  {profiles.length === 0 && <option value="minimal">minimal(默认)</option>}
                  {profiles.map((p) => (
                    <option key={p.name} value={p.name}>
                      {p.name} · {p.description}
                    </option>
                  ))}
                </select>
              </label>
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
                  {workspaceProjects.length > 0 && (
                    <p className="hint">受影响项目可选；不选时后续可在代码变更阶段再指定</p>
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
            disabled={(isNew && (!key.trim() || !workspace.trim() || !content.trim())) || loading}
            onClick={handleConfirm}
          >
            {loading ? '处理中...' : '准备 PRD 并进入规划'}
          </button>
        </div>
      </div>
    </div>
  )
}
