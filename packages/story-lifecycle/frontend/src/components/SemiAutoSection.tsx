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

  // 读已回填的 session id(用户终端里的 agent 调 story session --writeback 回写后才有)。
  // 没回填时 session_id=null,resume 按钮禁用(提示用户先跑回写)。
  const { data: sessionRow } = useQuery<{ session_id: string | null; adapter: string; stage: string }>({
    queryKey: ['session', storyKey],
    queryFn: async () => {
      const r = await fetch(`/api/story/${storyKey}/session`)
      if (!r.ok) throw new Error('load session failed')
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

  // 复制 resume 文案。不同 CLI 指令不同:
  //   claude: --resume <id>(或 -c 续当前目录最近会话)
  //   kimi:   -S <id>        (或 --session <id>; -c 续当前目录最近会话)
  // 已回写 id → 生成可直接执行的命令(按回写的 adapter 选 CLI)。
  // 未回写 → 给带占位符的模板 + 两种 CLI 都列出来,让用户填 id 自己选。
  async function copyResumeCmd() {
    const sid = sessionRow?.session_id
    const adapter = sessionRow?.adapter || ''
    const ws = ctx?.story?.workspace || ''
    const cwdHint = ws
      ? `# 请先 cd 到 story 工作区(claude 的 --resume 按 cwd 查找 transcript):\ncd "${ws}"\n`
      : '# 请在 story 工作区目录里执行(claude 的 --resume 按 cwd 查找):\n'
    let text: string
    if (sid) {
      // 已回写:按回写的 adapter 生成确切命令。
      const cmd = adapter === 'kimi' ? `kimi -S ${sid}` : `claude --resume ${sid}`
      text =
        cwdHint +
        `# 续接 ${storyKey} 的 ${sessionRow?.stage || ''} 会话(${adapter})\n` +
        cmd
    } else {
      // 未回写:给模板 + 两 CLI 都列,用户填 id 自己选。
      text =
        cwdHint +
        `# 续接 ${storyKey} 会话 —— 把 <id> 换成你的会话 id:\n` +
        `#   claude:  claude --resume <id>     (id 从 ~/.claude/projects/ 最新 jsonl 文件名取)\n` +
        `#   kimi:    kimi -S <id>             (id 从启动 banner 的 Session: session_xxx 取)\n` +
        `#   或不填 id:claude -c / kimi -c    (续当前目录最近一次会话)\n`
    }
    await navigator.clipboard.writeText(text)
    flash('resume')
  }

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
        <button className="btn" onClick={copyResumeCmd}>
          {copiedTarget === 'resume'
            ? '已复制 resume 文案'
            : sessionRow?.session_id
              ? `复制 resume 命令（${sessionRow.adapter}）`
              : '复制 resume 文案（未回写,填 id 用）'}
        </button>
        <button className="btn" onClick={copyReleasePrompt}>
          {copiedTarget === 'release' ? '已复制上线提示词' : '复制上线准备提示词'}
        </button>
        <button className="btn" onClick={copyPostReleasePrompt}>
          {copiedTarget === 'post-release' ? '已复制验证提示词' : '已经上线 · 自动验证'}
        </button>
      </div>
    </details>
  )
}
