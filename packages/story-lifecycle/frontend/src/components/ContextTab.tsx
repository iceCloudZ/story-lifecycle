import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'

interface ContextDoc { kind: string; ref: string; summary?: string }
interface ContextChange { kind: string; ref: string; summary?: string; evidence_ref?: string }
interface ContextBundle {
  story: { title?: string; tapd_url?: string; profile?: string; current_stage?: string; workspace?: string }
  story_projects: { project_id: number; branch?: string; worktree_path?: string; base_branch?: string; summary?: string }[]
  projects: { id: number; name?: string }[]
  documents: ContextDoc[]
  change_items: ContextChange[]
  delivery_artifacts: { kind?: string; url?: string; target_branch?: string }[]
  revision: number
}

interface StagePrompt { stage: string; path: string; content: string }
interface PromptsResponse { story_key: string; prompts: StagePrompt[] }

export default function ContextTab({ storyKey }: { storyKey: string }) {
  const [copied, setCopied] = useState(false)
  const [copiedTarget, setCopiedTarget] = useState('')
  const [skill, setSkill] = useState('')

  const { data: ctx } = useQuery<ContextBundle>({
    queryKey: ['context', storyKey],
    queryFn: async () => {
      const r = await fetch(`/api/story/${storyKey}/context`)
      if (!r.ok) throw new Error('load context failed')
      return r.json()
    },
  })

  const { data: promptsData } = useQuery<PromptsResponse>({
    queryKey: ['prompts', storyKey],
    queryFn: async () => {
      const r = await fetch(`/api/story/${storyKey}/prompts`)
      if (!r.ok) throw new Error('load prompts failed')
      return r.json()
    },
  })

  async function copyPack() {
    const url = skill
      ? `/api/story/${storyKey}/context/pack?skill=${encodeURIComponent(skill)}`
      : `/api/story/${storyKey}/context/pack`
    const r = await fetch(url)
    const body = await r.json()
    await navigator.clipboard.writeText(body.content || '')
    setCopied(true)
    setCopiedTarget('pack')
    setTimeout(() => setCopied(false), 2000)
    setTimeout(() => setCopiedTarget(''), 2000)
  }

  async function copyReleasePrompt() {
    const r = await fetch(`/api/story/${storyKey}/context/release-prompt`, { method: 'POST' })
    const body = await r.json()
    await navigator.clipboard.writeText(body.content || '')
    setCopiedTarget('release')
    setTimeout(() => setCopiedTarget(''), 2000)
  }

  async function copyPostReleasePrompt() {
    const r = await fetch(`/api/story/${storyKey}/context/post-release-prompt`, { method: 'POST' })
    const body = await r.json()
    await navigator.clipboard.writeText(body.content || '')
    setCopiedTarget('post-release')
    setTimeout(() => setCopiedTarget(''), 2000)
  }

  async function copyText(value: string, target: string) {
    if (!value) return
    await navigator.clipboard.writeText(value)
    setCopiedTarget(target)
    setTimeout(() => setCopiedTarget(''), 2000)
  }

  function openLocalPath(path: string) {
    if (!path) return
    window.open(`file:///${path.replace(/\\/g, '/')}`, '_blank', 'noopener,noreferrer')
  }

  const projName = (pid: number) => ctx?.projects.find((p) => p.id === pid)?.name || '(未知项目)'
  const ddl = (ctx?.change_items || []).filter((c) => c.kind === 'ddl')
  const nacos = (ctx?.change_items || []).filter((c) => c.kind === 'nacos')
  const workspace = ctx?.story?.workspace || ''
  const prd = (ctx?.documents || []).find((d) => d.kind === 'prd')
  const prdPath = prd?.ref || ''

  return (
    <div className="context-tab">
      <div className="ctx-toolbar">
        <button className="btn btn-primary" onClick={copyPack}>
          {copied && copiedTarget === 'pack' ? '已复制资料包' : '复制上下文资料包'}
        </button>
        <button className="btn" onClick={copyReleasePrompt}>
          {copiedTarget === 'release' ? '已复制上线提示词' : '复制上线准备提示词'}
        </button>
        <button className="btn" onClick={copyPostReleasePrompt}>
          {copiedTarget === 'post-release' ? '已复制验证提示词' : '已经上线 · 自动验证'}
        </button>
        <button className="btn" disabled={!prdPath} onClick={() => copyText(prdPath, 'prd')}>
          {copiedTarget === 'prd' ? '已复制 PRD 路径' : '复制 PRD 路径'}
        </button>
        <button className="btn" disabled={!workspace} onClick={() => copyText(workspace, 'workspace')}>
          {copiedTarget === 'workspace' ? '已复制工作区' : '复制工作区'}
        </button>
        <button className="btn" disabled={!prdPath} onClick={() => openLocalPath(prdPath)}>
          打开 PRD
        </button>
        <select value={skill} onChange={(e) => setSkill(e.target.value)} className="ctx-skill-select">
          <option value="">（中性，不指定 skill）</option>
          <option value="bug-fix">bug-fix</option>
          <option value="env-debug">env-debug</option>
        </select>
      </div>

      <section className="ctx-manual-panel">
        <h4>半自动使用</h4>
        <div className="ctx-steps">
          <div>1. 确认工作区和 PRD 路径正确。</div>
          <div>2. 复制上下文资料包，粘贴给你手动打开的 AI agent。</div>
          <div>3. 让 AI 先做 Design/影响模块分析；确认后再决定是否进入开发。</div>
        </div>
      </section>

      <section>
        <h4>工作区</h4>
        <div className="ctx-item ctx-path">{workspace || '(未选择工作区)'}</div>
      </section>

      <section>
        <h4>绑定项目与分支</h4>
        {(ctx?.story_projects || []).length === 0 && (
          <p className="ctx-empty">未绑定项目。半自动流程下这是正常状态，影响模块和分支在 Design 后再确定。</p>
        )}
        {(ctx?.story_projects || []).map((sp) => (
          <div key={sp.project_id} className="ctx-item">
            <strong>{projName(sp.project_id)}</strong>：分支 <code>{sp.branch || '-'}</code>
            {sp.base_branch && <span> （基线 {sp.base_branch}）</span>}
            {sp.summary && <div className="ctx-sub">{sp.summary}</div>}
          </div>
        ))}
      </section>

      <section>
        <h4>文档（{ctx?.documents?.length || 0}）</h4>
        {(ctx?.documents || []).map((d, i) => (
          <div key={i} className="ctx-item">
            <strong>{d.kind}</strong>：<span className="ctx-path">{d.ref || '(无路径)'}</span>
            {d.summary && <div className="ctx-sub">{d.summary}</div>}
          </div>
        ))}
      </section>

      {(promptsData?.prompts || []).length > 0 && (
        <section>
          <h4>提示词复盘（{(promptsData?.prompts || []).length} 个 stage）</h4>
          {(promptsData?.prompts || []).map((p) => (
            <details key={p.stage} className="ctx-prompt-item">
              <summary>
                <strong>{p.stage}</strong>
                <button
                  className="btn btn-sm"
                  onClick={(e) => {
                    e.preventDefault()
                    copyText(p.content, `prompt-${p.stage}`)
                  }}
                >
                  {copiedTarget === `prompt-${p.stage}` ? '已复制' : '复制'}
                </button>
              </summary>
              <pre className="ctx-prompt-content">{p.content}</pre>
            </details>
          ))}
        </section>
      )}

      <section>
        <h4>DDL（{ddl.length}） · Nacos（{nacos.length}）</h4>
        {ddl.map((c, i) => (
          <div key={`d${i}`} className="ctx-item"><strong>DDL</strong>：{c.ref} {c.summary && <span className="ctx-sub">— {c.summary}</span>}</div>
        ))}
        {nacos.map((c, i) => (
          <div key={`n${i}`} className="ctx-item"><strong>Nacos</strong>：{c.ref} {c.summary && <span className="ctx-sub">— {c.summary}</span>}</div>
        ))}
      </section>

      <section>
        <h4>交付产物（{ctx?.delivery_artifacts?.length || 0}）</h4>
        {(ctx?.delivery_artifacts || []).map((da, i) => (
          <div key={i} className="ctx-item"><strong>{da.kind}</strong>：{da.url} {da.target_branch && <span className="ctx-sub">→ {da.target_branch}</span>}</div>
        ))}
      </section>
    </div>
  )
}
