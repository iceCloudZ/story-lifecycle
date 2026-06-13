import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { storyApi } from '../api/client'

interface Props {
  storyKey: string
}

export default function QualityGateTab({ storyKey }: Props) {
  const [subTab, setSubTab] = useState<'findings' | 'gate'>('findings')

  const { data: findingsData } = useQuery({
    queryKey: ['findings', storyKey],
    queryFn: () => storyApi.findings(storyKey),
    enabled: !!storyKey,
  })

  const { data: gateHistory } = useQuery({
    queryKey: ['gateHistory', storyKey],
    queryFn: () => storyApi.gateHistory(storyKey),
    enabled: !!storyKey,
  })

  const findings = findingsData?.findings ?? []
  const decisions = gateHistory?.decisions ?? []

  // Sort findings by severity
  const severityOrder: Record<string, number> = { high: 0, medium: 1, low: 2 }
  const sortedFindings = [...findings].sort(
    (a, b) => (severityOrder[a.severity] ?? 3) - (severityOrder[b.severity] ?? 3)
  )

  return (
    <div className="tab-content quality-gate-tab">
      {/* Sub-tabs */}
      <div className="qgt-subtabs">
        <button
          className={`qgt-subtab ${subTab === 'findings' ? 'active' : ''}`}
          onClick={() => setSubTab('findings')}
        >
          Findings ({findings.length})
        </button>
        <button
          className={`qgt-subtab ${subTab === 'gate' ? 'active' : ''}`}
          onClick={() => setSubTab('gate')}
        >
          Gate 决策 ({decisions.length})
        </button>
      </div>

      {subTab === 'findings' && (
        <div className="qgt-findings">
          {sortedFindings.length === 0 ? (
            <div className="qgt-empty">暂无 Findings</div>
          ) : (
            sortedFindings.map((f: any, i: number) => (
              <div key={f.id || i} className={`qgt-finding qgt-sev-${f.severity}`}>
                <span className="qgt-finding-sev">[{f.severity?.toUpperCase() || 'LOW'}]</span>
                <span className="qgt-finding-cat">{f.category || '--'}</span>
                <span className="qgt-finding-desc">{f.description || '--'}</span>
                <span className={`qgt-finding-status qgt-status-${f.status}`}>{f.status || 'open'}</span>
              </div>
            ))
          )}
        </div>
      )}

      {subTab === 'gate' && (
        <div className="qgt-gate-list">
          {decisions.length === 0 ? (
            <div className="qgt-empty">暂无 Gate 决策</div>
          ) : (
            decisions.map((d: any, i: number) => (
              <div key={i} className={`qgt-gate-item qgt-gate-${d.decision}`}>
                <span className="qgt-gate-decision">{d.decision}</span>
                <span className="qgt-gate-stage">{d.stage}</span>
                <span className="qgt-gate-reason">{d.reason_code || d.human_message || '--'}</span>
              </div>
            ))
          )}
        </div>
      )}
    </div>
  )
}
