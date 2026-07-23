import { useState, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Diff, Hunk, parseDiff } from 'react-diff-view'
import 'react-diff-view/style/index.css'
import { diffApi, storyApi } from '../api/client'
import type { DiffFile, StoryProject, Project } from '../api/client'
import './CodeChangesTab.css'

interface Props {
  storyKey: string
}

/** worktree_state → 显示文案 + 颜色类。 */
function worktreeBadge(state: string): { label: string; cls: string } {
  switch (state) {
    case 'available':
      return { label: 'worktree', cls: 'wt-available' }
    case 'unprepared':
      return { label: '未就绪', cls: 'wt-unprepared' }
    case 'missing':
      return { label: '丢失', cls: 'wt-missing' }
    case 'stale':
      return { label: '过期', cls: 'wt-missing' }
    case 'conflict':
      return { label: '冲突', cls: 'wt-missing' }
    default:
      return { label: state || '未知', cls: 'wt-unprepared' }
  }
}

export default function CodeChangesTab({ storyKey }: Props) {
  const [expandedFile, setExpandedFile] = useState<string | null>(null)

  // 拉项目绑定(切换器 + diff 定位用)。context 端点已存在,这里只用 story_projects +
  // projects 两档。
  const { data: ctx } = useQuery({
    queryKey: ['context', storyKey],
    queryFn: () => storyApi.context(storyKey),
    enabled: !!storyKey,
  })

  const projectsById = useMemo(() => {
    const m = new Map<number, Project>()
    for (const p of ctx?.projects ?? []) m.set(Number(p.id), p)
    return m
  }, [ctx?.projects])

  const bindings: StoryProject[] = ctx?.story_projects ?? []
  const hasMultiple = bindings.length >= 2

  // 选中项目(默认首个绑定)。单项目/无绑定时为 undefined,走旧 storyKey-only diff。
  const [selectedProjectId, setSelectedProjectId] = useState<number | undefined>(
    undefined,
  )
  const effectiveProjectId = hasMultiple
    ? (selectedProjectId ?? bindings[0]?.project_id)
    : undefined

  const { data: diff, isLoading, error } = useQuery({
    queryKey: ['diff', storyKey, effectiveProjectId],
    queryFn: () => diffApi.get(storyKey, effectiveProjectId),
    enabled: !!storyKey,
    // diff 是重查询(每仓 fetch ~2s),且 story 详情页进页时会并行 prefetch 所有
    // project 的 diff。这里给较长 staleTime,避免在 project 间切换 / tab 切回时重拉
    // (全局默认 5s 对 diff 太短 — 来回切 tab 会反复 fetch)。
    staleTime: 2 * 60 * 1000,
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
  const fellBackToRepo = !diff.worktree_path && !!diff.repo_path

  return (
    <div className="tab-content code-changes-tab">
      {hasMultiple && (
        <div className="cct-project-switcher">
          {bindings.map((b) => {
            const pid = b.project_id
            const proj = projectsById.get(pid)
            const active = pid === effectiveProjectId
            const badge = worktreeBadge(b.worktree_state)
            return (
              <button
                key={b.id}
                type="button"
                className={`cct-project-chip${active ? ' active' : ''}`}
                onClick={() => setSelectedProjectId(pid)}
                title={b.worktree_path || `repo: ${proj?.repo_path ?? ''}`}
              >
                <span className="cct-project-name">{proj?.name ?? `#${pid}`}</span>
                {b.branch && <span className="cct-project-branch">{b.branch}</span>}
                <span className={`cct-wt-badge ${badge.cls}`}>{badge.label}</span>
              </button>
            )
          })}
        </div>
      )}

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

      {diff.worktree_path ? (
        <div className="cct-worktree-info">
          <span className="cct-worktree-label">worktree:</span>
          <code className="cct-worktree-path">{diff.worktree_path}</code>
        </div>
      ) : (
        fellBackToRepo && (
          <div className="cct-worktree-info warn">
            <span className="cct-worktree-label">⚠ worktree 未就绪,显示主仓 diff:</span>
            <code className="cct-worktree-path">{diff.repo_path}</code>
          </div>
        )
      )}

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
