import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import type {
  WorkspaceEntity,
  WorkspaceEntityDetail,
  WorkspaceScenario,
  WorkspaceProject,
} from '../api/client'
import './lifecycle/LifecyclePage.css'
import './WorkspacePage.css'

/**
 * 工作区 — 业务项目实体(11-workspace-entity-design.md Phase 2)。
 * 顶层路由,与 Board 平级。Phase 2 只读:旅程 / Stories / 概览 三 tab;
 * Wiki tab 随 Phase 3(知识层 type: wiki 条目)落地。
 */

const TABS = [
  { id: 'journeys', label: '旅程' },
  { id: 'stories', label: 'Stories' },
  { id: 'overview', label: '概览' },
] as const

type TabId = (typeof TABS)[number]['id']

export default function WorkspacePage() {
  const [workspaces, setWorkspaces] = useState<WorkspaceEntity[]>([])
  const [selected, setSelected] = useState<WorkspaceEntity | null>(null)
  const [detail, setDetail] = useState<WorkspaceEntityDetail | null>(null)
  const [tab, setTab] = useState<TabId>('journeys')
  const [showCreate, setShowCreate] = useState(false)

  function loadList() {
    fetch('/api/workspace-entities').then(r => r.json()).then((d: { workspaces: WorkspaceEntity[] }) => {
      const list = d.workspaces || []
      setWorkspaces(list)
      if (list.length > 0 && !selected) setSelected(list[0])
      else if (list.length === 0) setSelected(null)
    })
  }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { loadList() }, [])

  useEffect(() => {
    if (!selected) { setDetail(null); return }
    let cancelled = false
    fetch(`/api/workspace-entities/${selected.slug}`).then(r => r.json()).then((d: WorkspaceEntityDetail) => {
      if (!cancelled) setDetail(d)
    })
    return () => { cancelled = true }
  }, [selected])

  function handleCreate(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault()
    const form = new FormData(e.currentTarget)
    const name = String(form.get('name') || '').trim()
    if (!name) return
    fetch('/api/workspace-entities', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name,
        slug: String(form.get('slug') || '').trim() || undefined,
      }),
    }).then(r => {
      if (r.ok) { setShowCreate(false); loadList() }
      else r.json().then(err => alert('创建失败: ' + (err.detail || '未知错误')))
    })
  }

  return (
    <div className="lifecycle-page">
      <div className="lifecycle-header">
        <h2>工作区</h2>
        <span className="story-count">{workspaces.length} 个业务项目</span>
        <div className="page-actions">
          <button className="btn btn-primary" onClick={() => setShowCreate(!showCreate)}>
            {showCreate ? '取消' : '新建工作区'}
          </button>
        </div>
      </div>

      {showCreate && (
        <form className="create-form" onSubmit={handleCreate}>
          <input name="name" placeholder="业务项目名 (如 HappyCash 授信域)" required />
          <input name="slug" placeholder="slug (如 hc-credit-domain, 可选)" />
          <button type="submit" className="btn btn-primary">创建</button>
          <button type="button" className="btn" onClick={() => setShowCreate(false)}>取消</button>
        </form>
      )}

      {workspaces.length === 0 ? (
        <div className="ui-empty" style={{ padding: 40, textAlign: 'center' }}>
          <p>尚未创建 Workspace 实体</p>
          <p className="ui-hint" style={{ marginTop: 4 }}>
            不创建时行为与之前完全一致;创建后用
            <span className="ws-mono"> story workspace init &lt;name&gt; --repo name=path </span>
            跑初始化管线
          </p>
        </div>
      ) : (
        <>
          <div className="ui-chip-row" role="tablist" aria-label="工作区选择">
            {workspaces.map(ws => (
              <button
                key={ws.slug}
                role="tab"
                aria-selected={selected?.slug === ws.slug}
                className={`ui-chip ${selected?.slug === ws.slug ? 'active' : ''}`}
                onClick={() => setSelected(ws)}
              >
                {ws.name}
              </button>
            ))}
          </div>

          {selected && (
            <div className="ws-tabs" role="tablist" aria-label="工作区视图">
              {TABS.map(t => (
                <button
                  key={t.id}
                  role="tab"
                  aria-selected={tab === t.id}
                  className={`ui-chip ${tab === t.id ? 'active' : ''}`}
                  onClick={() => setTab(t.id)}
                >
                  {t.label}
                </button>
              ))}
            </div>
          )}

          {detail && <WorkspaceBody tab={tab} detail={detail} />}
        </>
      )}
    </div>
  )
}

function WorkspaceBody({ tab, detail }: { tab: TabId; detail: WorkspaceEntityDetail }) {
  const ws = detail.workspace
  const initState = ws.init_state || {}
  return (
    <div className="ws-body">
      {tab === 'journeys' && <JourneysTab scenarios={detail.scenarios} />}
      {tab === 'stories' && <StoriesTab stories={detail.stories} />}
      {tab === 'overview' && (
        <OverviewTab
          repos={detail.repos}
          integrations={ws.integrations || {}}
          initState={initState}
          knowledgeRoot={ws.knowledge_root || ''}
        />
      )}
    </div>
  )
}

// ---- 旅程 tab:scenario 条目投影(D6:不存 wiki 页,无同步问题) ----

function JourneysTab({ scenarios }: { scenarios: WorkspaceScenario[] }) {
  return (
    <div className="ui-card ui-card-pad">
      <div className="ui-section-title">测试旅程(Scenario 投影)</div>
      {scenarios.length === 0 ? (
        <div className="ui-empty" style={{ marginTop: 12 }}>
          <p>暂无 scenario 条目</p>
          <p className="ui-hint" style={{ marginTop: 4 }}>
            知识层生成后自动出现:story workspace init --step init_scenarios
          </p>
        </div>
      ) : (
        <div className="ui-list">
          {scenarios.map(sc => (
            <div className="ui-list-row" key={sc.id}>
              <span className="ws-svg-icon" aria-hidden>
                <JourneyIcon />
              </span>
              <div className="ws-row-main">
                <div className="ws-row-title">
                  {sc.title || sc.id}
                  {sc.status && (
                    <span className={`badge-type ${sc.status === 'confirmed' ? 'badge-ok' : 'badge-warn'}`}>
                      {sc.status}
                    </span>
                  )}
                </div>
                <div className="ws-row-meta">
                  <span className="ws-mono">{sc.id}</span>
                  {sc.domain && <span>{sc.domain}</span>}
                  {sc.apis && sc.apis.length > 0 && <span>{sc.apis.length} 个 API</span>}
                  {sc.updated_at && <span>{sc.updated_at.slice(0, 10)}</span>}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// ---- Stories tab:该 Workspace 下所有 story(经 Repo → story_project 反查) ----

function StoriesTab({ stories }: { stories: WorkspaceEntityDetail['stories'] }) {
  const navigate = useNavigate()
  return (
    <div className="ui-card ui-card-pad">
      <div className="ui-section-title">Stories(经 Repo 反查)</div>
      {stories.length === 0 ? (
        <div className="ui-empty" style={{ marginTop: 12 }}>
          <p>该 Workspace 下还没有 story</p>
        </div>
      ) : (
        <div className="ui-list">
          {stories.map(s => (
            <div className="ui-list-row clickable" key={s.storyKey} onClick={() => navigate(`/story/${s.storyKey}`)}>
              <div className="ws-row-main">
                <div className="ws-row-title">
                  {s.title || s.storyKey}
                  <span className={`badge-type ${s.lifecycleState === '结项' ? 'badge-ok' : 'badge-warn'}`}>
                    {s.lifecycleState || s.status}
                  </span>
                </div>
                <div className="ws-row-meta">
                  <span className="ws-mono">{s.storyKey}</span>
                  <span>{s.currentStage}</span>
                  <span>{s.updatedAt?.slice(0, 10)}</span>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// ---- 概览 tab:Repo + 运行时事实 + 集成 + 初始化状态 ----

function OverviewTab({ repos, integrations, initState, knowledgeRoot }: {
  repos: WorkspaceProject[]
  integrations: Record<string, unknown>
  initState: Record<string, unknown>
  knowledgeRoot: string
}) {
  const gitlab = integrations.gitlab as { url?: string } | undefined
  const ci = integrations.ci as { provider?: string } | undefined
  return (
    <div className="ws-overview">
      <div className="ui-card ui-card-pad">
        <div className="ui-section-title">Repos({repos.length})</div>
        {repos.length === 0 ? (
          <div className="ui-hint" style={{ marginTop: 8 }}>
            尚未注册仓库:story workspace init --repo name=path
          </div>
        ) : (
          <div className="ui-list">
            {repos.map(p => (
              <div className="ui-list-row" key={String(p.id)}>
                <span className="ws-svg-icon" aria-hidden><RepoIcon /></span>
                <div className="ws-row-main">
                  <div className="ws-row-title">
                    {p.name}
                    <span className={`badge-type ${p.availability === 'available' ? 'badge-ok' : 'badge-warn'}`}>
                      {p.availability || 'unknown'}
                    </span>
                  </div>
                  <div className="ws-row-meta">
                    <span className="ws-mono">{p.repo_path}</span>
                    <span>分支 {p.default_branch}</span>
                    {(p.runtime_facts || []).map(f => (
                      <span key={f.runtime_type}>{f.runtime_type}</span>
                    ))}
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
        {knowledgeRoot && (
          <p className="ui-hint" style={{ marginTop: 8 }}>知识根: <span className="ws-mono">{knowledgeRoot}</span></p>
        )}
      </div>

      <div className="ui-card ui-card-pad">
        <div className="ui-section-title">集成(登记状态,Phase 2 只展示)</div>
        <div className="ws-integration-row">
          <span className="ws-svg-icon" aria-hidden><GitlabIcon /></span>
          <span>GitLab</span>
          <span className="ws-mono">{gitlab?.url || '未登记'}</span>
        </div>
        <div className="ws-integration-row">
          <span className="ws-svg-icon" aria-hidden><CiIcon /></span>
          <span>CI</span>
          <span className="ws-mono">{ci?.provider || '未登记'}</span>
        </div>
      </div>

      <div className="ui-card ui-card-pad">
        <div className="ui-section-title">初始化管线(§3)</div>
        <div className="ws-init-steps">
          {['register_repos', 'detect_runtime', 'gen_wiki', 'register_integrations', 'init_scenarios'].map(step => {
            const raw = initState[step]
            const status = typeof raw === 'string' ? raw : (raw as { status?: string } | undefined)?.status || 'pending'
            const reason = typeof raw === 'object' && raw ? (raw as { reason?: string }).reason : ''
            return (
              <div key={step} className="ws-init-step">
                <span className={`ws-dot ${status}`} aria-hidden />
                <span className="ws-mono">{step}</span>
                <span className={`ws-init-status ${status}`}>{status}</span>
                {reason && <span className="ws-init-reason">{reason}</span>}
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}

// ---- Icons(描边 SVG,遵循 frontend/AGENTS.md 规则) ----

function JourneyIcon() {
  return (
    <svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="4" cy="4" r="1.6" />
      <circle cx="12" cy="8" r="1.6" />
      <circle cx="5" cy="12.5" r="1.6" />
      <path d="M5.5 4.7 L10.5 7.3 M10.5 8.7 L6 11.6" />
    </svg>
  )
}

function RepoIcon() {
  return (
    <svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
      <rect x="2.5" y="2.5" width="11" height="11" rx="1.6" />
      <path d="M5.5 5.5 H10.5 M5.5 8 H8.5" />
    </svg>
  )
}

function GitlabIcon() {
  return (
    <svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
      <path d="M2.5 9.2 L4.2 3.6 L6.4 8.8 H3.8 L2.5 9.2 Z M13.5 9.2 L11.8 3.6 L9.6 8.8 H12.2 L13.5 9.2 Z" />
      <path d="M2.5 9.2 L8 13.6 L13.5 9.2" />
    </svg>
  )
}

function CiIcon() {
  return (
    <svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="8" cy="8" r="5.5" />
      <path d="M8 5.5 V8 L10 9.5" />
    </svg>
  )
}
