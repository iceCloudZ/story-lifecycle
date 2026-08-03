import { useEffect, useState } from 'react'
import MarkdownView from './MarkdownView'

/**
 * WikiTab — WorkspacePage 的 Wiki tab 内容(11-workspace-entity-design.md §4/§5, Phase 3)。
 *
 * 独立组件,**不挂载**——挂载由汇总窗口做(phases/README.md 交界工作 b)。
 * 功能:wiki 条目列表(draft/merged 徽章)+ 正文渲染 + review 收件箱
 * (draft → approve/reject,§4.3 人工确认)。
 */

export interface WikiEntryItem {
  id: string
  title: string
  summary?: string
  source?: string
  review_state?: string
  related?: string[]
  evidence_refs?: Array<{ probe?: string; query?: string; observed_at?: string }>
  verified_at?: string
  review_reason?: string
  content?: string
  updated_at?: string
}

export default function WikiTab({ slug }: { slug: string }) {
  const [entries, setEntries] = useState<WikiEntryItem[]>([])
  const [selected, setSelected] = useState<WikiEntryItem | null>(null)
  const [rejectReason, setRejectReason] = useState('')
  const [busy, setBusy] = useState(false)

  function load() {
    fetch(`/api/workspace-entities/${slug}/wiki`).then(r => r.json()).then((d: { wiki: WikiEntryItem[] }) => {
      setEntries(d.wiki || [])
      setSelected(null)
    })
  }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { load() }, [slug])

  function review(id: string, decision: string, reason = '') {
    setBusy(true)
    fetch(`/api/workspace-entities/${slug}/wiki/${encodeURIComponent(id)}/review`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ decision, reviewer: 'user', reason }),
    }).then(r => {
      if (!r.ok) return r.json().then(err => alert('操作失败: ' + (err.detail || '未知错误')))
      setRejectReason('')
      load()
    }).finally(() => setBusy(false))
  }

  const drafts = entries.filter(e => e.review_state === 'draft')

  return (
    <div className="wiki-tab">
      <div className="wiki-layout">
        <div className="wiki-list">
          <div className="ui-section-title">条目({entries.length}) · draft {drafts.length}</div>
          {entries.length === 0 ? (
            <div className="ui-empty" style={{ marginTop: 12, padding: 24 }}>
              <p>暂无 wiki 条目</p>
              <p className="ui-hint" style={{ marginTop: 4 }}>
                跑 <span className="wiki-mono">story workspace init --step gen_wiki</span> 生成 L1 probe draft
              </p>
            </div>
          ) : (
            <div className="ui-list">
              {entries.map(e => (
                <div
                  key={e.id}
                  className={`ui-list-row clickable ${selected?.id === e.id ? 'wiki-row-active' : ''}`}
                  onClick={() => setSelected(e)}
                >
                  <div className="wiki-row-main">
                    <div className="wiki-row-title">
                      {e.title || e.id}
                      <span className={`badge-type ${e.review_state === 'merged' ? 'badge-ok' : 'badge-warn'}`}>
                        {e.review_state === 'merged' ? '已生效' : '待确认'}
                      </span>
                    </div>
                    <div className="wiki-row-meta">
                      <span className="wiki-mono">{e.id}</span>
                      {e.source && <span>{e.source}</span>}
                      {e.updated_at && <span>{e.updated_at.slice(0, 10)}</span>}
                    </div>
                    {e.summary && <p className="wiki-row-summary">{e.summary}</p>}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="wiki-detail ui-card ui-card-pad">
          {!selected ? (
            <div className="ui-empty" style={{ padding: 32, textAlign: 'center' }}>
              <p>选择左侧条目查看正文</p>
            </div>
          ) : (
            <>
              <div className="wiki-detail-head">
                <h4>{selected.title || selected.id}</h4>
                <span className={`badge-type ${selected.review_state === 'merged' ? 'badge-ok' : 'badge-warn'}`}>
                  {selected.review_state === 'merged' ? '已生效' : '待确认'}
                </span>
              </div>
              <p className="wiki-meta">
                <span className="wiki-mono">{selected.id}</span>
                <span>来源: {selected.source || 'human'}</span>
                {selected.verified_at && <span>确认于 {selected.verified_at.slice(0, 10)}</span>}
              </p>

              {selected.summary && (
                <div className="wiki-summary">
                  <div className="ui-section-title">摘要(agent 注入)</div>
                  <p>{selected.summary}</p>
                </div>
              )}

              {selected.related && selected.related.length > 0 && (
                <div className="wiki-related">
                  <div className="ui-section-title">相关交叉链接</div>
                  {selected.related.map(r => (
                    <span key={r} className="wiki-chip">{r}</span>
                  ))}
                </div>
              )}

              {selected.evidence_refs && selected.evidence_refs.length > 0 && (
                <div className="wiki-evidence">
                  <div className="ui-section-title">证据链(§5.3)</div>
                  {selected.evidence_refs.map((ev, i) => (
                    <p key={i} className="wiki-hint-line">
                      probe: <span className="wiki-mono">{ev.probe || '?'}</span>
                      {ev.query && <> · query: <span className="wiki-mono">{ev.query}</span></>}
                      {ev.observed_at && <> · {ev.observed_at.slice(0, 10)}</>}
                    </p>
                  ))}
                </div>
              )}

              <div className="wiki-body">
                <div className="ui-section-title">正文</div>
                <MarkdownView content={selected.content || ''} />
              </div>

              {selected.review_state === 'draft' && (
                <div className="wiki-review-box">
                  <div className="ui-section-title">Review 收件箱(§4.3)</div>
                  <div className="wiki-review-actions">
                    <button className="btn btn-primary" disabled={busy} onClick={() => review(selected.id, 'approve')}>
                      确认生效
                    </button>
                    <input
                      className="wiki-reject-input"
                      placeholder="打回原因(可选)"
                      value={rejectReason}
                      onChange={e => setRejectReason(e.target.value)}
                    />
                    <button className="btn" disabled={busy} onClick={() => review(selected.id, 'reject', rejectReason)}>
                      打回
                    </button>
                  </div>
                  {selected.review_reason && (
                    <p className="wiki-hint-line">上次打回原因: {selected.review_reason}</p>
                  )}
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  )
}
