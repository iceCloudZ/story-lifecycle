import { useQuery } from '@tanstack/react-query'
import { storyApi } from '../api/client'
import { useStories } from '../hooks/useStories'
import './DiagnosticsPage.css'

export default function DiagnosticsPage() {
  const { stories } = useStories()

  const failedStories = stories.filter((s) =>
    s.status === 'failed' || s.status === 'blocked'
  )

  return (
    <div className="diagnostics-page">
      <h2>系统诊断</h2>

      {/* Global Diagnostics */}
      <section className="diag-section">
        <h3>API 健康</h3>
        <div className="diag-item">
          <span className="diag-label">状态</span>
          <span className="diag-value diag-ok">在线</span>
        </div>
        <div className="diag-item">
          <span className="diag-label">活跃 Story</span>
          <span className="diag-value">{(stories ?? []).filter((s) => s.status === 'active').length}</span>
        </div>
        <div className="diag-item">
          <span className="diag-label">失败/阻塞</span>
          <span className="diag-value diag-warn">{failedStories.length}</span>
        </div>
        <div className="diag-item">
          <span className="diag-label">总 Story</span>
          <span className="diag-value">{(stories || []).length}</span>
        </div>
      </section>

      {/* Per-Story Diagnostics */}
      <section className="diag-section">
        <h3>失败/阻塞的 Story ({failedStories.length})</h3>
        {failedStories.length > 0 ? (
          failedStories.map((s) => (
            <StoryDiagnostics key={s.storyKey} storyKey={s.storyKey} />
          ))
        ) : (
          <p className="diag-empty">无失败或阻塞的 Story</p>
        )}
      </section>
    </div>
  )
}

function StoryDiagnostics({ storyKey }: { storyKey: string }) {
  const { data: debug } = useQuery({
    queryKey: ['debug', storyKey],
    queryFn: () => storyApi.debug(storyKey, 20),
  })
  const stuckReasons = debug?.stuckReasons ?? []
  const recentEvents = debug?.recentEvents ?? []

  function handleDownload() {
    const blob = new Blob([JSON.stringify(debug, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `debug-${storyKey}.json`
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div className="story-diag">
      <div className="diag-story-header">
        <h4>{storyKey}</h4>
        <button className="btn btn-sm" onClick={handleDownload}>
          下载 Debug Packet
        </button>
      </div>

      {stuckReasons.length > 0 && (
        <div className="diag-stuck">
          <span className="diag-label">Stuck Reasons:</span>
          <div className="stuck-tags">
            {stuckReasons.map((r, i) => (
              <span key={i} className="stuck-tag">{r}</span>
            ))}
          </div>
        </div>
      )}

      {recentEvents.length > 0 && (
        <div className="diag-events">
          <span className="diag-label">最近事件:</span>
          <div className="events-timeline">
            {recentEvents.slice(0, 10).map((ev, i) => (
              <div key={i} className={`diag-event ev-${ev.event_type}`}>
                <span className="ev-type">{ev.event_type}</span>
                <span className="ev-stage">{ev.stage}</span>
                {ev.detail && <span className="ev-detail">{truncate(ev.detail, 100)}</span>}
                <span className="ev-time">{formatTime(ev.created_at ?? '')}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {debug?.state && (
        <details className="diag-state">
          <summary>完整状态</summary>
          <pre>{JSON.stringify(debug.state, null, 2)}</pre>
        </details>
      )}
    </div>
  )
}

function truncate(s: string, max: number): string {
  if (!s) return ''
  return s.length > max ? s.slice(0, max) + '...' : s
}

function formatTime(t: string): string {
  if (!t) return ''
  try {
    return new Date(t).toLocaleTimeString()
  } catch {
    return t
  }
}
