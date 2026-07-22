import { useState, useEffect } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import type { Project } from '../api/client'
import './lifecycle/LifecyclePage.css'
import './ProjectsPage.css'

/**
 * 项目 — monorepo 项目注册管理(从 Dashboard 的项目 tab 抽出)。
 */
export default function ProjectsPage() {
  const qc = useQueryClient()
  const [showProjectForm, setShowProjectForm] = useState(false)
  const [projectCount, setProjectCount] = useState(0)

  return (
    <div className="lifecycle-page">
      <div className="lifecycle-header">
        <h2>项目</h2>
        <span className="story-count">{projectCount} 个项目</span>
        <div className="page-actions">
          <button className="btn btn-primary" onClick={() => setShowProjectForm(!showProjectForm)}>
            {showProjectForm ? '取消' : '注册项目'}
          </button>
        </div>
      </div>

      <ProjectPanel
        showForm={showProjectForm}
        setShowForm={setShowProjectForm}
        onCountChange={setProjectCount}
        onRefresh={() => qc.invalidateQueries({ queryKey: ['stories'] })}
      />
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
