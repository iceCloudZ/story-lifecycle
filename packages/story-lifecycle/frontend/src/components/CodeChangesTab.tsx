import { useState, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Diff, Hunk, parseDiff } from 'react-diff-view'
import 'react-diff-view/style/index.css'
import { diffApi } from '../api/client'
import type { DiffFile } from '../api/client'
import './CodeChangesTab.css'

interface Props {
  storyKey: string
}

export default function CodeChangesTab({ storyKey }: Props) {
  const [expandedFile, setExpandedFile] = useState<string | null>(null)

  const { data: diff, isLoading, error } = useQuery({
    queryKey: ['diff', storyKey],
    queryFn: () => diffApi.get(storyKey),
    enabled: !!storyKey,
  })

  const fileDiffs = useMemo(() => {
    if (!diff?.diff) return new Map<string, string>()
    const map = new Map<string, string>()
    const lines = diff.diff.split('\n')
    let currentFile = ''
    let buffer: string[] = []

    const flush = () => {
      if (currentFile && buffer.length) {
        map.set(currentFile, buffer.join('\n'))
      }
    }

    for (const line of lines) {
      if (line.startsWith('diff --git')) {
        flush()
        buffer = [line]
        const match = line.match(/diff --git a\/(.+?) b\/(.+)/)
        currentFile = match ? match[2] : line
      } else if (currentFile) {
        buffer.push(line)
      }
    }
    flush()
    return map
  }, [diff?.diff])

  if (isLoading) return <div className="cct-loading">加载 diff 中...</div>
  if (error) return <div className="cct-error">加载失败: {(error as Error).message}</div>
  if (!diff) return null

  const empty = diff.is_empty || diff.files.length === 0

  return (
    <div className="tab-content code-changes-tab">
      <div className="cct-header">
        <div className="cct-title">
          {diff.source === 'gitlab' && diff.mr_url ? (
            <a href={diff.mr_url} target="_blank" rel="noreferrer" className="cct-mr-link">
              MR !{diff.mr_iid}
            </a>
          ) : (
            <span>本地 diff</span>
          )}
          <span className="cct-branch">
            {diff.base_branch} ← {diff.current_branch}
          </span>
        </div>
        <div className="cct-actions">
          {diff.source === 'gitlab' && diff.mr_url && (
            <a
              className="btn btn-primary"
              href={diff.mr_url}
              target="_blank"
              rel="noreferrer"
            >
              在 GitLab 中查看
            </a>
          )}
        </div>
      </div>

      <div className="cct-stats">
        <div className="cct-stat">
          <div className="cct-stat-num">{diff.files.length}</div>
          <div className="cct-stat-label">文件变更</div>
        </div>
        <div className="cct-stat">
          <div className="cct-stat-num" style={{ color: '#3fb950' }}>+{diff.total_additions}</div>
          <div className="cct-stat-label">新增行</div>
        </div>
        <div className="cct-stat">
          <div className="cct-stat-num" style={{ color: '#f85149' }}>-{diff.total_deletions}</div>
          <div className="cct-stat-label">删除行</div>
        </div>
      </div>

      {empty ? (
        <div className="cct-empty">暂无代码变更。</div>
      ) : (
        <div className="cct-file-list">
          {diff.files.map((f: DiffFile) => (
            <div key={f.path} className="cct-file-item">
              <div
                className="cct-file-header"
                onClick={() => setExpandedFile(expandedFile === f.path ? null : f.path)}
              >
                <span className="cct-expand-icon">{expandedFile === f.path ? '▼' : '▶'}</span>
                <span className="cct-file-path">{f.path}</span>
                {f.additions > 0 && <span className="cct-file-additions">+{f.additions}</span>}
                {f.deletions > 0 && <span className="cct-file-deletions">-{f.deletions}</span>}
              </div>
              {expandedFile === f.path && (
                <FileDiffView diffText={fileDiffs.get(f.path) || diff.diff} />
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

interface FileDiffViewProps {
  diffText: string
}

function FileDiffView({ diffText }: FileDiffViewProps) {
  const files = parseDiff(diffText)
  if (!files.length) return <div className="cct-diff-empty">无 diff 内容</div>

  return (
    <div className="cct-diff-container">
      {files.map((file) => (
        <Diff key={file.newPath || file.oldPath} viewType="unified" diffType={file.type} hunks={file.hunks}>
          {(hunks) => hunks.map((hunk) => <Hunk key={hunk.content} hunk={hunk} />)}
        </Diff>
      ))}
    </div>
  )
}
