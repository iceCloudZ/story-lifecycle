import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { clarifyApi } from '../api/client'
import type { ClarifyQuestion } from '../api/client'
import './ClarifyDialog.css'

/**
 * design 阶段「逐问澄清」HITL 对话(runbook 块4)。
 *
 * claude 遇关键歧义写 clarify_request.json 后暂停(status=awaiting-clarify);
 * 本组件轮询 GET /clarify 取当前待答问题 → 用户选选项/自定义答 → POST /clarify/answer
 * → 后端累计 history + 重驱动 claude(带 Q&A 重启,前答影响后问)。下一问出现时再次展示,
 * 形成动态对话流,直到 claude 收敛写 design.json(status 离开 awaiting-clarify)。
 *
 * 用 react-query 轮询(与详情页 refetchInterval 一致);SSE 端点 /clarify/stream 亦可用。
 */
export default function ClarifyDialog({
  storyKey,
  status,
}: {
  storyKey: string
  status: string
}) {
  const qc = useQueryClient()
  const [custom, setCustom] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  const { data } = useQuery({
    queryKey: ['clarify', storyKey],
    queryFn: () => clarifyApi.get(storyKey),
    enabled: status === 'awaiting-clarify',
    refetchInterval: status === 'awaiting-clarify' ? 2000 : false,
  })

  if (status !== 'awaiting-clarify' || !data?.waiting || !data.question) return null
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
