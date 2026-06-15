import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'

interface ContextDoc { kind: string; ref: string; summary?: string }
interface ContextChange { kind: string; ref: string; summary?: string; evidence_ref?: string }
interface ContextBundle {
  story: { title?: string; tapd_url?: string; profile?: string; current_stage?: string }
  story_projects: { project_id: number; branch?: string; worktree_path?: string; base_branch?: string; summary?: string }[]
  projects: { id: number; name?: string }[]
  documents: ContextDoc[]
  change_items: ContextChange[]
  delivery_artifacts: { kind?: string; url?: string; target_branch?: string }[]
  revision: number
}

export default function ContextTab({ storyKey }: { storyKey: string }) {
  const [copied, setCopied] = useState(false)

  const { data: ctx } = useQuery<ContextBundle>({
    queryKey: ['context', storyKey],
    queryFn: async () => {
      const r = await fetch(`/api/story/${storyKey}/context`)
      if (!r.ok) throw new Error('load context failed')
      return r.json()
    },
  })

  async function copyPack() {
    const r = await fetch(`/api/story/${storyKey}/context/pack`)
    const body = await r.json()
    await navigator.clipboard.writeText(body.content || '')
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  const projName = (pid: number) => ctx?.projects.find((p) => p.id === pid)?.name || '(未知项目)'
  const ddl = (ctx?.change_items || []).filter((c) => c.kind === 'ddl')
  const nacos = (ctx?.change_items || []).filter((c) => c.kind === 'nacos')

  return (
    <div className="context-tab">
      <div className="ctx-toolbar">
        <button className="btn btn-primary" onClick={copyPack}>
          {copied ? '✓ 已复制' : '复制上下文资料包'}
        </button>
        <span className="ctx-hint">粘贴到任意 AI agent 即可（开发/改 bug/排查通用）</span>
      </div>

      <section>
        <h4>绑定项目与分支</h4>
        {(ctx?.story_projects || []).length === 0 && <p className="ctx-empty">未绑定项目</p>}
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
            <strong>{d.kind}</strong>：{d.ref || '(无路径)'}
            {d.summary && <div className="ctx-sub">{d.summary}</div>}
          </div>
        ))}
      </section>

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