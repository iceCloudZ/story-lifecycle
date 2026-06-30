import { useState, Suspense, lazy } from 'react'
import { useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { diffApi } from '../api/client'
import { parseUnifiedDiff } from '../utils/diffParser'
import './DiffPreviewPage.css'

const GitDiffViewViewer = lazy(() => import('../components/diff-viewers/GitDiffViewViewer'))
const ReactDiffViewViewer = lazy(() => import('../components/diff-viewers/ReactDiffViewViewer'))
const CodeMirrorMergeViewer = lazy(() => import('../components/diff-viewers/CodeMirrorMergeViewer'))
const MonacoDiffViewer = lazy(() => import('../components/diff-viewers/MonacoDiffViewer'))

const TABS = [
  { id: 'git-diff-view', label: '@git-diff-view/react', component: GitDiffViewViewer },
  { id: 'react-diff-view', label: 'react-diff-view', component: ReactDiffViewViewer },
  { id: 'codemirror', label: 'CodeMirror Merge', component: CodeMirrorMergeViewer },
  { id: 'monaco', label: 'Monaco Diff', component: MonacoDiffViewer },
]

export default function DiffPreviewPage() {
  const { key } = useParams<{ key: string }>()
  const [activeTab, setActiveTab] = useState(TABS[0].id)
  const [selectedIndex, setSelectedIndex] = useState(0)

  const { data: diffResp, isLoading, error } = useQuery({
    queryKey: ['diff', key],
    queryFn: () => diffApi.get(key || ''),
    enabled: !!key,
  })

  if (isLoading) return <div className="dpp-loading">加载 diff...</div>
  if (error) return <div className="dpp-error">加载失败: {(error as Error).message}</div>
  if (!diffResp) return null

  const files = parseUnifiedDiff(diffResp.diff)
  const currentFile = files[selectedIndex] || files[0]

  return (
    <div className="dpp-page">
      <div className="dpp-header">
        <h2>Diff Viewer 预览 — {key}</h2>
        <div className="dpp-meta">
          <span>来源: {diffResp.source}</span>
          {diffResp.mr_url && (
            <a href={diffResp.mr_url} target="_blank" rel="noreferrer">
              MR !{diffResp.mr_iid}
            </a>
          )}
          <span>文件数: {files.length}</span>
          <label className="dpp-file-select">
            当前文件:
            <select
              value={selectedIndex}
              onChange={(e) => setSelectedIndex(Number(e.target.value))}
            >
              {files.map((f, i) => (
                <option key={i} value={i}>
                  {f.newPath || f.oldPath}
                </option>
              ))}
            </select>
          </label>
        </div>
      </div>

      <div className="dpp-tabs">
        {TABS.map((tab) => (
          <button
            key={tab.id}
            className={`dpp-tab ${activeTab === tab.id ? 'active' : ''}`}
            onClick={() => setActiveTab(tab.id)}
          >
            {tab.label}
          </button>
        ))}
      </div>

      <div className="dpp-content">
        {!currentFile ? (
          <div className="dpp-empty">没有可预览的 diff</div>
        ) : (
          TABS.map((tab) => {
            const Component = tab.component
            return (
              <div
                key={tab.id}
                className={`dpp-panel ${activeTab === tab.id ? 'active' : ''}`}
              >
                {activeTab === tab.id && (
                  <Suspense fallback={<div className="dpp-loading">加载组件...</div>}>
                    <Component file={currentFile} />
                  </Suspense>
                )}
              </div>
            )
          })
        )}
      </div>
    </div>
  )
}
