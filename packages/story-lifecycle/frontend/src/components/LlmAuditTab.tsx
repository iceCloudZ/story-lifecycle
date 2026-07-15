import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { auditApi } from '../api/client'
import type { LlmCallRow } from '../api/client'
import './LlmAuditTab.css'

export default function LlmAuditTab({ storyKey }: { storyKey: string }) {
  const [copiedId, setCopiedId] = useState('')
  const [stageFilter, setStageFilter] = useState('')
  const [modelFilter, setModelFilter] = useState('')
  const [failOnly, setFailOnly] = useState(false)

  const { data, isLoading, error } = useQuery({
    queryKey: ['llm-calls', storyKey],
    queryFn: () => auditApi.calls(storyKey),
  })

  const calls = data?.calls ?? []

  // 筛选项去重（从数据派生，不硬编码）
  const stages = useMemo(
    () => [...new Set(calls.map((c) => c.stage).filter(Boolean))] as string[],
    [calls],
  )
  const models = useMemo(
    () => [...new Set(calls.map((c) => c.model).filter(Boolean))] as string[],
    [calls],
  )

  const filtered = useMemo(
    () =>
      calls.filter((c) => {
        if (stageFilter && c.stage !== stageFilter) return false
        if (modelFilter && c.model !== modelFilter) return false
        if (failOnly && c.success !== 0) return false
        return true
      }),
    [calls, stageFilter, modelFilter, failOnly],
  )

  const failCount = calls.filter((c) => c.success === 0).length
  const totalTokens = calls.reduce((s, c) => s + (c.total_tokens ?? 0), 0)

  async function copyText(text: string, id: string) {
    try {
      await navigator.clipboard.writeText(text)
      setCopiedId(id)
      setTimeout(() => setCopiedId(''), 1500)
    } catch {
      /* clipboard 不可用时静默 */
    }
  }

  /** prompt_text 存的是 messages 数组 JSON，尽量 pretty-print；失败则原样展示。 */
  function prettyPrompt(raw?: string | null): string {
    if (!raw) return ''
    try {
      return JSON.stringify(JSON.parse(raw), null, 2)
    } catch {
      return raw
    }
  }

  if (isLoading) return <div className="llm-audit-loading">加载中…</div>
  if (error) return <div className="llm-audit-error">加载失败：{(error as Error).message}</div>

  return (
    <div className="llm-audit">
      <div className="llm-audit-toolbar">
        <select value={stageFilter} onChange={(e) => setStageFilter(e.target.value)}>
          <option value="">全部阶段（{calls.length}）</option>
          {stages.map((s) => (
            <option key={s} value={s}>{s}</option>
          ))}
        </select>
        <select value={modelFilter} onChange={(e) => setModelFilter(e.target.value)}>
          <option value="">全部模型</option>
          {models.map((m) => (
            <option key={m} value={m}>{m}</option>
          ))}
        </select>
        <label className="llm-audit-check">
          <input type="checkbox" checked={failOnly} onChange={(e) => setFailOnly(e.target.checked)} />
          仅失败（{failCount}）
        </label>
        <span className="llm-audit-summary">
          共 {filtered.length}/{calls.length} 条 · Token {(totalTokens / 1000).toFixed(1)}K
        </span>
      </div>

      {filtered.length === 0 && (
        <div className="llm-audit-empty">暂无 LLM 调用记录</div>
      )}

      {filtered.map((c) => (
        <CallDetails key={c.id} call={c} copiedId={copiedId} onCopy={copyText} prettyPrompt={prettyPrompt} />
      ))}
    </div>
  )
}

function CallDetails({
  call,
  copiedId,
  onCopy,
  prettyPrompt,
}: {
  call: LlmCallRow
  copiedId: string
  onCopy: (text: string, id: string) => void
  prettyPrompt: (raw?: string | null) => string
}) {
  const prompt = prettyPrompt(call.prompt_text)
  const failed = call.success === 0
  const time = call.created_at ? new Date(call.created_at).toLocaleString('zh-CN') : ''

  return (
    <details className={`llm-call-item${failed ? ' llm-call-failed' : ''}`}>
      <summary>
        <span className="llm-call-stage">{call.stage || call.operation || '(无阶段)'}</span>
        <span className="llm-call-model">{call.model || '?'}</span>
        <span className="llm-call-tokens">
          {call.total_tokens ?? 0} tok · {call.duration_ms ?? 0}ms
        </span>
        {failed && <span className="llm-call-badge-fail">失败</span>}
        {call.reasoning_text && <span className="llm-call-badge-reason">含思考</span>}
        <span className="llm-call-time">{time}</span>
      </summary>

      <div className="llm-call-bodies">
        {failed && call.error && (
          <div className="llm-call-body llm-call-body-error">
            <div className="llm-call-body-head">
              错误
              <button className="btn btn-sm" onClick={() => onCopy(call.error || '', `err-${call.id}`)}>
                {copiedId === `err-${call.id}` ? '已复制' : '复制'}
              </button>
            </div>
            <pre className="llm-call-pre">{call.error}</pre>
          </div>
        )}

        <div className="llm-call-body">
          <div className="llm-call-body-head">
            Prompt（{call.prompt_tokens ?? 0} tok）
            <button className="btn btn-sm" onClick={() => onCopy(prompt, `prompt-${call.id}`)}>
              {copiedId === `prompt-${call.id}` ? '已复制' : '复制'}
            </button>
          </div>
          <pre className="llm-call-pre">{prompt || '(空)'}</pre>
        </div>

        {call.reasoning_text && (
          <div className="llm-call-body llm-call-body-reason">
            <div className="llm-call-body-head">
              思考过程
              <button className="btn btn-sm" onClick={() => onCopy(call.reasoning_text || '', `reason-${call.id}`)}>
                {copiedId === `reason-${call.id}` ? '已复制' : '复制'}
              </button>
            </div>
            <pre className="llm-call-pre">{call.reasoning_text}</pre>
          </div>
        )}

        <div className="llm-call-body">
          <div className="llm-call-body-head">
            Response（{call.completion_tokens ?? 0} tok）
            <button className="btn btn-sm" onClick={() => onCopy(call.response_text || '', `resp-${call.id}`)}>
              {copiedId === `resp-${call.id}` ? '已复制' : '复制'}
            </button>
          </div>
          <pre className="llm-call-pre">{call.response_text || '(空)'}</pre>
        </div>

        {call.tool_calls_json && call.tool_calls_json !== '[]' && (
          <div className="llm-call-body">
            <div className="llm-call-body-head">
              Tool Calls
              <button className="btn btn-sm" onClick={() => onCopy(call.tool_calls_json || '', `tc-${call.id}`)}>
                {copiedId === `tc-${call.id}` ? '已复制' : '复制'}
              </button>
            </div>
            <pre className="llm-call-pre">{prettyPrompt(call.tool_calls_json)}</pre>
          </div>
        )}
      </div>
    </details>
  )
}
