import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'

interface ContextDoc { kind: string; ref: string; summary?: string }
interface ContextBundle {
  story: { workspace?: string }
  documents: ContextDoc[]
}

/**
 * 半自动工具(原 ContextTab 收敛版):只保留复制类操作,收起在概览底部的
 * <details> 里。提示词复盘/文档/DDL/交付产物等信息型 section 已下线。
 */
export default function SemiAutoSection({ storyKey }: { storyKey: string }) {
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

  function flash(target: string) {
    setCopiedTarget(target)
    setTimeout(() => setCopiedTarget(''), 2000)
  }

  async function copyPack() {
    const url = skill
      ? `/api/story/${storyKey}/context/pack?skill=${encodeURIComponent(skill)}`
      : `/api/story/${storyKey}/context/pack`
    const r = await fetch(url)
    const body = await r.json()
    await navigator.clipboard.writeText(body.content || '')
    flash('pack')
  }

  async function copyReleasePrompt() {
    const r = await fetch(`/api/story/${storyKey}/context/release-prompt`, { method: 'POST' })
    const body = await r.json()
    await navigator.clipboard.writeText(body.content || '')
    flash('release')
  }

  async function copyPostReleasePrompt() {
    const r = await fetch(`/api/story/${storyKey}/context/post-release-prompt`, { method: 'POST' })
    const body = await r.json()
    await navigator.clipboard.writeText(body.content || '')
    flash('post-release')
  }

  async function copyText(value: string, target: string) {
    if (!value) return
    await navigator.clipboard.writeText(value)
    flash(target)
  }

  function openLocalPath(path: string) {
    if (!path) return
    window.open(`file:///${path.replace(/\\/g, '/')}`, '_blank', 'noopener,noreferrer')
  }

  const workspace = ctx?.story?.workspace || ''
  const prd = (ctx?.documents || []).find((d) => d.kind === 'prd')
  const prdPath = prd?.ref || ''

  return (
    <details className="semi-auto-section">
      <summary className="semi-auto-summary">🛠 半自动工具</summary>
      <div className="semi-auto-toolbar">
        <button className="btn btn-primary" onClick={copyPack}>
          {copiedTarget === 'pack' ? '已复制资料包' : '复制上下文资料包'}
        </button>
        <select value={skill} onChange={(e) => setSkill(e.target.value)} className="semi-auto-skill-select">
          <option value="">（中性，不指定 skill）</option>
          <option value="bug-fix">bug-fix</option>
          <option value="env-debug">env-debug</option>
        </select>
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
      </div>
    </details>
  )
}
