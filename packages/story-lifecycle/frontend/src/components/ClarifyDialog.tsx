import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { clarifyApi } from '../api/client'
import type { ClarifyQuestion } from '../api/client'
import './ClarifyDialog.css'

/**
 * design 阶段「逐问澄清」HITL 对话(外接 MCP 方案,runbook 块4)。
 *
 * claude 遇关键歧义调 mcp__lifecycle__clarify → MCP server 落 clarification_request 事件 +
 * 阻塞等人答(同一 claude 进程,不重 spawn)。本组件轮询 GET /clarify 取待答问题 → 用户答 →
 * POST /clarify/answer 落 clarification_answer 事件 → MCP server 解除 claude 阻塞 → claude
 * 带答继续。下一问出现时再展示,直到 claude 收敛写 design.json。
 *
 * 显示条件:data.waiting(status 仍 active——claude 阻塞在 MCP 调用上,非特殊 status)。
 */
const RUNNING = new Set(['planning', 'active', 'implementing'])

export default function ClarifyDialog({
  storyKey,
  status,
  headless,
}: {
  storyKey: string
  status: string
  headless?: boolean
}) {
  const qc = useQueryClient()
  const [custom, setCustom] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  // BUG #9:交互式路径(headless=false)走"终端直接问人",不走 MCP clarify。
  // 该路径不轮询 /clarify、不显示卡片(避免空卡片 + 无谓请求)。
  const running = RUNNING.has(status) && headless !== false
  const { data } = useQuery({
    queryKey: ['clarify', storyKey],
    queryFn: () => clarifyApi.get(storyKey),
    enabled: running,
    refetchInterval: running ? 3000 : false,
  })

  if (!headless) return null
  if (!data?.waiting || !data.question) return null
  const q: ClarifyQuestion = data.question

  async function submit(answer: string) {
    const a = answer.trim()
    if (!a || submitting) return
    setSubmitting(true)
    setError('')
    try {
      await clarifyApi.answer(storyKey, a, q.id)
      setCustom('')
      // 状态会翻 active(claude 重启);轮询 story 让本组件自然隐藏/再显示下一问
      qc.invalidateQueries({ queryKey: ['story', storyKey] })
      qc.invalidateQueries({ queryKey: ['clarify', storyKey] })
    } catch (e) {
      setError(String(e))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="clarify-dialog">
      <div className="cd-header">
        <span className="cd-icon">❓</span>
        <span className="cd-title">设计澄清 · 需要你拍板</span>
        <span className="cd-hint">claude 遇关键岔路,答完它带你的回答继续设计</span>
      </div>
      <div className="cd-question">
        <div className="cd-q-header">{q.header || q.question}</div>
        <div className="cd-q-body">{q.question}</div>
        {q.context && <div className="cd-q-context">{q.context}</div>}
      </div>
      <div className="cd-options">
        {q.options.map((opt) => (
          <button
            key={opt}
            className="cd-option"
            disabled={submitting}
            onClick={() => submit(opt)}
          >
            {opt}
          </button>
        ))}
      </div>
      <div className="cd-custom">
        <input
          value={custom}
          onChange={(e) => setCustom(e.target.value)}
          placeholder="或自定义回答…(回车送出)"
          disabled={submitting}
          onKeyDown={(e) => {
            if (e.key === 'Enter') submit(custom)
          }}
        />
        <button
          className="cd-send"
          disabled={submitting || !custom.trim()}
          onClick={() => submit(custom)}
        >
          送出
        </button>
      </div>
      {error && <div className="cd-error">提交失败:{error}</div>}
      {submitting && (
        <div className="cd-status">已提交,claude 正在基于回答继续…</div>
      )}
    </div>
  )
}
