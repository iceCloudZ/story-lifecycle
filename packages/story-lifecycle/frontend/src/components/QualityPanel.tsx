import { useQuery } from '@tanstack/react-query'
import { storyApi, type GateDecision } from '../api/client'
import './QualityPanel.css'

/**
 * 质量门禁面板（迭代 2 P2-UI）：gate 决策时间线 + findings + repair_action。
 *
 * 数据源：GET /api/story/{key}/gate-history（gate_decision 事件 + stage_completion
 * 编排决策 + gate_result 表合并）。空态显示「尚未经过质量判定」。
 */
export default function QualityPanel({ storyKey }: { storyKey: string }) {
  const { data, isLoading } = useQuery({
    queryKey: ['gate-history', storyKey],
    queryFn: () => storyApi.gateHistory(storyKey),
    refetchInterval: 10000,
  })

  const decisions: GateDecision[] = data?.decisions ?? []

  if (isLoading) {
    return (
      <div className="ui-card qp-panel" data-testid="qp-panel">
        <h3 className="ui-section-title qp-title">质量门禁</h3>
        <p className="ui-hint">加载中…</p>
      </div>
    )
  }

  if (decisions.length === 0) {
    return (
      <div className="ui-card qp-panel" data-testid="qp-panel">
        <h3 className="ui-section-title qp-title">质量门禁</h3>
        <p className="ui-hint" data-testid="qp-empty">尚未经过质量判定</p>
      </div>
    )
  }

  const findings = decisions.flatMap((d) => (d.findings ?? []).map((f) => ({ ...f, _stage: d.stage, _at: d.created_at })))
  const highFindings = findings.filter((f) => (f.severity || '').toLowerCase() === 'high')
  const ordered = [...findings].sort((a, b) => {
    const sev = (x: string | undefined) => ({ high: 0, medium: 1, low: 2 } as Record<string, number>)[(x || '').toLowerCase()] ?? 3
    return sev(a.severity) - sev(b.severity)
  })

  return (
    <div className="ui-card qp-panel" data-testid="qp-panel">
      <h3 className="ui-section-title qp-title">
        质量门禁
        {highFindings.length > 0 && <span className="badge badge-danger qp-high-count" data-testid="qp-high-count">{highFindings.length} HIGH</span>}
      </h3>

      <div className="qp-section">
        <h4 className="ui-section-title qp-sub">决策时间线</h4>
        <ul className="ui-list qp-decisions" data-testid="qp-decisions">
          {decisions.map((d, i) => (
            <li key={i} className={`ui-list-row qp-decision ${d.fallback ? 'qp-fallback' : ''} ${(d.decision || d.verdict || '').toLowerCase() === 'escalate' ? 'qp-escalate' : ''}`} data-testid="qp-decision">
              <span className="qp-decision-badge badge">{d.decision || d.verdict || '?'}</span>
              <span className="qp-decision-stage">{d.stage || ''}</span>
              {d.fallback && <span className="badge badge-warning qp-fb-tag" data-testid="qp-fallback-tag">[FALLBACK]</span>}
              {d.repair_action?.kind && (
                <span className="badge qp-repair-tag" data-testid="qp-repair">{d.repair_action.kind}</span>
              )}
              <span className="qp-decision-reason">{(d.human_message || d.reason_code || '').slice(0, 160)}</span>
              <span className="qp-decision-at">{(d.created_at || '').slice(0, 19)}</span>
            </li>
          ))}
        </ul>
      </div>

      {ordered.length > 0 && (
        <div className="qp-section">
          <h4 className="ui-section-title qp-sub">Findings（{ordered.length}）</h4>
          <ul className="ui-list qp-findings" data-testid="qp-findings">
            {ordered.map((f, i) => (
              <li key={i} className={`ui-list-row qp-finding qp-sev-${(f.severity || 'low').toLowerCase()}`} data-testid="qp-finding">
                <span className={`badge ${(f.severity || '').toLowerCase() === 'high' ? 'badge-danger' : (f.severity || '').toLowerCase() === 'medium' ? 'badge-warning' : ''}`}>{f.severity || 'low'}</span>
                <span className="qp-finding-cat">{f.category || ''}</span>
                <span className="qp-finding-desc">{(f.description || '').slice(0, 200)}</span>
                {f.location && <span className="qp-finding-loc">@{f.location}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}
