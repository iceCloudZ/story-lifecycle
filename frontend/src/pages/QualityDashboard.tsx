import { useQuery, useQueryClient } from '@tanstack/react-query'
import { patternApi, storyApi } from '../api/client'
import type { Pattern, Story } from '../api/client'
import './QualityDashboard.css'

export default function QualityDashboard() {
  const qc = useQueryClient()

  const { data: proposed } = useQuery({
    queryKey: ['patterns', 'proposed'],
    queryFn: () => patternApi.list('proposed'),
  })

  const { data: active } = useQuery({
    queryKey: ['patterns', 'active'],
    queryFn: () => patternApi.list('active'),
  })

  const { data: rejected } = useQuery({
    queryKey: ['patterns', 'rejected'],
    queryFn: () => patternApi.list('rejected'),
  })

  const { data: stories } = useQuery({
    queryKey: ['stories'],
    queryFn: storyApi.list,
  })

  async function handleApprove(id: string | number) {
    const ok = await patternApi.approve(id)
    if (ok) {
      qc.invalidateQueries({ queryKey: ['patterns'] })
    }
  }

  async function handleReject(id: string | number) {
    const ok = await patternApi.reject(id)
    if (ok) {
      qc.invalidateQueries({ queryKey: ['patterns'] })
    }
  }

  // Aggregate findings stats
  const findingsStats = aggregateFindings(stories ?? [])
  const rejectedPatterns = rejected?.patterns ?? []

  return (
    <div className="quality-dashboard">
      <h2>Quality Dashboard</h2>

      {/* Finding Statistics */}
      <section className="qd-section">
        <h3>Findings 统计</h3>
        {findingsStats.total > 0 ? (
          <div className="findings-stats">
            <div className="stat-group">
              <span className="stat-label">总计</span>
              <span className="stat-value">{findingsStats.total}</span>
            </div>
            <div className="stat-group">
              <span className="stat-label severity-high">High</span>
              <span className="stat-value">{findingsStats.high}</span>
            </div>
            <div className="stat-group">
              <span className="stat-label severity-medium">Medium</span>
              <span className="stat-value">{findingsStats.medium}</span>
            </div>
            <div className="stat-group">
              <span className="stat-label severity-low">Low</span>
              <span className="stat-value">{findingsStats.low}</span>
            </div>
            <div className="stat-separator" />
            <div className="stat-group">
              <span className="stat-label">Open</span>
              <span className="stat-value">{findingsStats.open}</span>
            </div>
            <div className="stat-group">
              <span className="stat-label">Resolved</span>
              <span className="stat-value">{findingsStats.resolved}</span>
            </div>
          </div>
        ) : (
          <p className="qd-empty">暂无 Findings 数据</p>
        )}
        {Object.keys(findingsStats.byCategory).length > 0 && (
          <div className="category-breakdown">
            <span className="diag-label">按类别:</span>
            {Object.entries(findingsStats.byCategory).map(([cat, count]) => (
              <span key={cat} className="category-tag">{cat}: {count}</span>
            ))}
          </div>
        )}
      </section>

      {/* Proposed Patterns */}
      <section className="qd-section">
        <h3>待审批 Patterns ({proposed?.patterns?.length || 0})</h3>
        {proposed?.patterns?.length ? (
          <div className="pattern-list">
            {proposed.patterns.map((p: Pattern) => (
              <div key={p.id} className="pattern-card pattern-proposed">
                <div className="pattern-pattern">{p.pattern}</div>
                <div className="pattern-rule">{p.rule}</div>
                <div className="pattern-meta">
                  <span className="pattern-confidence">{p.confidence}</span>
                  <span className="pattern-applies">{p.applies_to?.join(', ')}</span>
                </div>
                <div className="pattern-actions">
                  <button className="btn btn-primary" onClick={() => handleApprove(p.id)}>批准</button>
                  <button className="btn btn-danger" onClick={() => handleReject(p.id)}>拒绝</button>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <p className="qd-empty">无待审批 Pattern</p>
        )}
      </section>

      {/* Active Patterns */}
      <section className="qd-section">
        <h3>活跃 Patterns ({active?.patterns?.length || 0})</h3>
        {active?.patterns?.length ? (
          <div className="pattern-list">
            {active.patterns.map((p: Pattern) => (
              <div key={p.id} className="pattern-card pattern-active">
                <div className="pattern-pattern">{p.pattern}</div>
                <div className="pattern-rule">{p.rule}</div>
                <div className="pattern-meta">
                  <span className="pattern-confidence">{p.confidence}</span>
                  <span className="pattern-applies">{p.applies_to?.join(', ')}</span>
                  <span className="pattern-verified">验证: {p.verification_count || 0}</span>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <p className="qd-empty">无活跃 Pattern</p>
        )}
      </section>

      {/* Rejected Patterns (collapsed) */}
      {rejectedPatterns.length > 0 && (
        <section className="qd-section">
          <details>
            <summary><h3 className="inline-h3">已拒绝 Patterns ({rejectedPatterns.length})</h3></summary>
            <div className="pattern-list">
              {rejectedPatterns.map((p: Pattern) => (
                <div key={p.id} className="pattern-card pattern-rejected">
                  <div className="pattern-pattern">{p.pattern}</div>
                  <div className="pattern-rule">{p.rule}</div>
                  <div className="pattern-meta">
                    <span className="pattern-confidence">{p.confidence}</span>
                    <span className="pattern-applies">{p.applies_to?.join(', ')}</span>
                  </div>
                </div>
              ))}
            </div>
          </details>
        </section>
      )}
    </div>
  )
}

function aggregateFindings(stories: Story[]) {
  const stats = {
    total: 0,
    high: 0,
    medium: 0,
    low: 0,
    open: 0,
    resolved: 0,
    byCategory: {} as Record<string, number>,
  }
  // This is a simplified aggregation based on story list data
  // In production, we'd fetch findings per story or add a summary API
  for (const s of stories) {
    if (s.findingsCount) stats.total += s.findingsCount
    if (s.openFindings) stats.open += s.openFindings
    if (s.highSeverityFindings) stats.high += s.highSeverityFindings
  }
  return stats
}
