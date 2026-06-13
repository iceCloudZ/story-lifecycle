import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { storyApi } from '../api/client'

interface Props {
  storyKey: string
}

interface FileChange {
  path: string
  additions: number
  deletions: number
  stage: string
  diff?: string
}

export default function CodeChangesTab({ storyKey }: Props) {
  const [stageFilter, setStageFilter] = useState('')
  const [expandedFile, setExpandedFile] = useState<string | null>(null)

  const { data: timeline } = useQuery({
    queryKey: ['timeline', storyKey],
    queryFn: () => storyApi.timeline(storyKey),
    enabled: !!storyKey,
  })

  // Build file change list from timeline data
  const allFiles: FileChange[] = []
  if (timeline?.stages) {
    for (const stage of timeline.stages) {
      const events = stage.events || []
      for (const ev of events) {
        if (ev.event_type === 'plan' && ev.summary) {
          // Parse files_changed from stage output if available
        }
      }
    }
  }

  const filteredFiles = stageFilter
    ? allFiles.filter(f => f.stage === stageFilter)
    : allFiles

  const totalAdditions = filteredFiles.reduce((s, f) => s + f.additions, 0)
  const totalDeletions = filteredFiles.reduce((s, f) => s + f.deletions, 0)

  // Extract unique stage names from timeline
  const stageNames: string[] = timeline?.stages?.map((s: any) => s.stage) ?? []

  return (
    <div className="tab-content code-changes-tab">
      {/* Stage filter */}
      <div className="cct-filters">
        <button
          className={`cct-filter-btn ${stageFilter === '' ? 'active' : ''}`}
          onClick={() => setStageFilter('')}
        >
          全部阶段
        </button>
        {stageNames.map((s: string) => (
          <button
            key={s}
            className={`cct-filter-btn ${stageFilter === s ? 'active' : ''}`}
            onClick={() => setStageFilter(s)}
          >
            {s}
          </button>
        ))}
      </div>

      {/* Stats bar */}
      <div className="cct-stats">
        <div className="cct-stat">
          <div className="cct-stat-num">{filteredFiles.length}</div>
          <div className="cct-stat-label">文件变更</div>
        </div>
        <div className="cct-stat">
          <div className="cct-stat-num" style={{ color: '#3fb950' }}>+{totalAdditions}</div>
          <div className="cct-stat-label">新增行</div>
        </div>
        <div className="cct-stat">
          <div className="cct-stat-num" style={{ color: '#f85149' }}>-{totalDeletions}</div>
          <div className="cct-stat-label">删除行</div>
        </div>
      </div>

      {/* File list */}
      {filteredFiles.length === 0 ? (
        <div className="cct-empty">暂无代码变更记录。阶段执行完成后会在这里展示 git diff。</div>
      ) : (
        <div className="cct-file-list">
          {filteredFiles.map((f, i) => (
            <div key={i} className="cct-file-item">
              <div
                className="cct-file-header"
                onClick={() => setExpandedFile(expandedFile === f.path ? null : f.path)}
              >
                <span className="cct-expand-icon">{expandedFile === f.path ? '▼' : '▶'}</span>
                <span className="cct-file-path">{f.path}</span>
                <span className="cct-file-additions">+{f.additions}</span>
                <span className="cct-file-deletions">-{f.deletions}</span>
                <span className="cct-file-stage">{f.stage}</span>
              </div>
              {expandedFile === f.path && f.diff && (
                <pre className="cct-diff">{f.diff}</pre>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
