import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { storyApi } from '../api/client'

interface Props {
  storyKey: string
}

export default function AdversarialLoopTab({ storyKey }: Props) {
  const [stageFilter, setStageFilter] = useState('')
  const [expandedRound, setExpandedRound] = useState<number | null>(null)

  const { data: loopTrace } = useQuery({
    queryKey: ['loopTrace', storyKey],
    queryFn: () => storyApi.loopTrace(storyKey),
    enabled: !!storyKey,
  })

  // Merge plan and code loop rounds
  const allRounds: any[] = []
  const addRounds = (rounds: any[], loopType: string) => {
    for (const r of rounds) {
      allRounds.push({ ...r, loopType })
    }
  }
  if (loopTrace?.plan_loop?.rounds) addRounds(loopTrace.plan_loop.rounds, 'plan')
  if (loopTrace?.code_loop?.rounds) addRounds(loopTrace.code_loop.rounds, 'code')

  const filteredRounds = stageFilter
    ? allRounds.filter(r => r.stage === stageFilter)
    : allRounds

  // Extract unique stages
  const stages = [...new Set(allRounds.map(r => r.stage))]

  return (
    <div className="tab-content loop-tab">
      {/* Stage filter */}
      <div className="lt-filters">
        <button
          className={`lt-filter-btn ${stageFilter === '' ? 'active' : ''}`}
          onClick={() => setStageFilter('')}
        >
          全部
        </button>
        {stages.map((s: string) => (
          <button
            key={s}
            className={`lt-filter-btn ${stageFilter === s ? 'active' : ''}`}
            onClick={() => setStageFilter(s)}
          >
            {s}
          </button>
        ))}
      </div>

      {filteredRounds.length === 0 ? (
        <div className="lt-empty">
          暂无对抗循环数据。阶段执行中会在这里展示每轮的 Plan↔Review / Code↔Review 对抗轨迹。
        </div>
      ) : (
        <div className="lt-rounds">
          {filteredRounds.map((r, i) => (
            <div key={i} className={`lt-card ${r.loop_decision ? `lt-${r.loop_decision}` : ''}`}>
              <div
                className="lt-card-header"
                onClick={() => setExpandedRound(expandedRound === i ? null : i)}
              >
                <span className="lt-round-num">
                  {r.stage} · Round {r.loop_rounds || i + 1}
                </span>
                <span className={`lt-badge lt-badge-${r.loop_decision || 'advance'}`}>
                  {r.loop_decision || 'advance'}
                </span>
                {r.trajectory_score != null && (
                  <span className="lt-score">评分: {r.trajectory_score}</span>
                )}
                <span className="lt-expand">{expandedRound === i ? '▼' : '▶'}</span>
              </div>
              {expandedRound === i && (
                <div className="lt-card-body">
                  <div className="lt-code-side">
                    <div className="lt-side-label">{r.loopType === 'plan' ? 'Plan' : 'Code'}</div>
                    <div className="lt-side-content">{r.summary || '--'}</div>
                  </div>
                  <div className="lt-arrow">→</div>
                  <div className="lt-review-side">
                    <div className="lt-side-label">Review</div>
                    <div className="lt-side-content">
                      质量: {r.quality || '--'}
                      {r.issues_count > 0 && ` · ${r.issues_count} issues`}
                    </div>
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
