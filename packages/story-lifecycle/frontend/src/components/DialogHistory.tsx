import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { auditApi, storyApi } from '../api/client'
import type { LlmCallRow, TimelineStage } from '../api/client'
import './DialogHistory.css'

interface Props {
  storyKey: string
  /** 只展示某个 stage 的记录(由 stage 终端框传入);空 = 全部。 */
  stage?: string
}

type SubTab = 'llm' | 'timeline'

/**
 * 对话历史面板 —— 终端区旁侧的"之前聊过什么"。
 *
 * 两个数据源,子 tab 切换(用户要两个都展示后再定):
 *   - 对话(llm-calls):每次 LLM 调用的 prompt/response/reasoning,逐条可展开。
 *     半自动(用户自己跑 CLI)通常没记录;全自动有。
 *   - 事件(timeline):stage 级事件流(plan/review/gate/error),较粗。
 *
 * stage prop 有值时只展示该 stage 的记录;否则全部。按 stage 分组展示。
 */
export default function DialogHistory({ storyKey, stage }: Props) {
  const [subTab, setSubTab] = useState<SubTab>('llm')

  const { data: llmData } = useQuery({
    queryKey: ['llm-calls', storyKey],
    queryFn: () => auditApi.calls(storyKey),
    enabled: !!storyKey,
    staleTime: 30 * 1000,
  })

  const { data: timelineData } = useQuery({
    queryKey: ['timeline', storyKey],
    queryFn: () => storyApi.timeline(storyKey),
    enabled: !!storyKey,
    staleTime: 30 * 1000,
  })

  const allCalls = llmData?.calls ?? []
  const allStages = timelineData?.stages ?? []

  // stage 过滤 + 分组
  const calls = stage ? allCalls.filter((c) => c.stage === stage) : allCalls
  const stages = stage ? allStages.filter((s) => s.stage === stage) : allStages

  // 按 stage 分组(保持出现顺序)
  const callGroups = groupByStage(calls)

  return (
    <div className="dh-panel">
      <div className="dh-subtabs">
        <button
          className={`dh-subtab ${subTab === 'llm' ? 'active' : ''}`}
          onClick={() => setSubTab('llm')}
        >
          对话 ({calls.length})
        </button>
        <button
          className={`dh-subtab ${subTab === 'timeline' ? 'active' : ''}`}
          onClick={() => setSubTab('timeline')}
        >
          事件 ({stages.length})
        </button>
      </div>

      <div className="dh-body">
        {subTab === 'llm' ? (
          <LlmCallList groups={callGroups} />
        ) : (
          <TimelineList stages={stages} />
        )}
      </div>
    </div>
  )
}

// 按 stage 分组,保持首次出现顺序;空 stage 归到 '(无阶段)'。
function groupByStage(calls: LlmCallRow[]): Array<{ stage: string; calls: LlmCallRow[] }> {
  const order: string[] = []
  const map: Record<string, LlmCallRow[]> = {}
  for (const c of calls) {
    const s = c.stage || '(无阶段)'
    if (!map[s]) {
      map[s] = []
      order.push(s)
    }
    map[s].push(c)
  }
  return order.map((s) => ({ stage: s, calls: map[s] }))
}

function LlmCallList({ groups }: { groups: Array<{ stage: string; calls: LlmCallRow[] }> }) {
  if (groups.length === 0) {
    return <p className="dh-empty">暂无 LLM 调用记录(半自动模式通常没有)。</p>
  }
  return (
    <div className="dh-list">
      {groups.map((g) => (
        <div key={g.stage} className="dh-stage-group">
          <div className="dh-stage-head">{g.stage}</div>
          {g.calls.map((c) => (
            <LlmCallItem key={c.id} call={c} />
          ))}
        </div>
      ))}
    </div>
  )
}

function LlmCallItem({ call }: { call: LlmCallRow }) {
  const [open, setOpen] = useState(false)
  const ok = call.success !== 0
  const tokens = call.total_tokens ?? 0
  return (
    <div className={`dh-call-item${open ? ' open' : ''}`}>
      <button className="dh-call-head" onClick={() => setOpen((v) => !v)}>
        <span className={`dh-call-dot ${ok ? 'ok' : 'fail'}`} />
        <span className="dh-call-op">{call.operation || 'call'}</span>
        <span className="dh-call-model">{call.model || ''}</span>
        {tokens > 0 && <span className="dh-call-tokens">{tokens} tok</span>}
        <span className="dh-call-time">{fmtTime(call.created_at)}</span>
        <span className="dh-call-chevron">{open ? '▾' : '▸'}</span>
      </button>
      {open && (
        <div className="dh-call-body">
          {call.error ? (
            <div className="dh-call-error">⚠ {call.error}</div>
          ) : null}
          {call.prompt_text && (
            <details>
              <summary>提示词</summary>
              <pre className="dh-pre">{truncate(call.prompt_text, 4000)}</pre>
            </details>
          )}
          {call.response_text && (
            <details open>
              <summary>响应</summary>
              <pre className="dh-pre">{truncate(call.response_text, 6000)}</pre>
            </details>
          )}
          {call.reasoning_text && (
            <details>
              <summary>思考</summary>
              <pre className="dh-pre">{truncate(call.reasoning_text, 4000)}</pre>
            </details>
          )}
        </div>
      )}
    </div>
  )
}

function TimelineList({ stages }: { stages: TimelineStage[] }) {
  if (stages.length === 0) {
    return <p className="dh-empty">暂无 stage 事件。</p>
  }
  return (
    <div className="dh-list">
      {stages.map((s) => (
        <div key={s.stage} className="dh-stage-group">
          <div className="dh-stage-head">{s.stage}</div>
          {(s.events ?? []).length === 0 ? (
            <p className="dh-empty dh-empty-inline">无关键事件</p>
          ) : (
            (s.events ?? []).map((e, i) => (
              <div key={i} className="dh-tl-event">
                <span className={`dh-tl-tag dh-tag-${e.event_type}`}>{e.event_type}</span>
                <span className="dh-tl-summary">{e.summary || '(无摘要)'}</span>
              </div>
            ))
          )}
        </div>
      ))}
    </div>
  )
}

function truncate(s: string | null | undefined, n: number): string {
  if (!s) return ''
  return s.length > n ? s.slice(0, n) + `\n…(截断,共 ${s.length} 字符)` : s
}

function fmtTime(t?: string): string {
  if (!t) return ''
  // 只取时分秒
  return t.slice(11, 19)
}
