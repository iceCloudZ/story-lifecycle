import { useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { docApi, type DocSearchHit } from '../api/client'
import './DocSearchPage.css'

/**
 * DocSearchPage — full-text search across all versioned docs (FTS5).
 * Results link into the story's docs tab.
 */

/**
 * 把 FTS5 snippet(匹配词被 [词] 包裹)安全地转成 React 节点 + <mark> 高亮。
 *
 * FTS5 用 `[` `]` 包匹配词。旧实现用 dangerouslySetInnerHTML 手拼 HTML(先转义再放回
 * <mark>),转义顺序脆弱,有 XSS 隐患。这里改成纯 React:文本天然由 React 转义,
 * 只在 [词] 处切分插入 <mark>。
 */
function renderSnippet(snippet: string): ReactNode[] {
  if (!snippet) return []
  // 按 [词] 拆分;奇数索引段 = 匹配词。
  const parts = snippet.split(/(\[[^\]]+\])/)
  return parts.map((part, i) => {
    if (part.startsWith('[') && part.endsWith(']')) {
      const inner = part.slice(1, -1)
      return <mark key={i}>{inner}</mark>
    }
    return <span key={i}>{part}</span>
  })
}

export default function DocSearchPage() {
  const [q, setQ] = useState('')
  const [submitted, setSubmitted] = useState('')

  const { data, isLoading } = useQuery({
    queryKey: ['doc-search', submitted],
    queryFn: () => docApi.search(submitted),
    enabled: !!submitted,
  })

  const results: DocSearchHit[] = data?.results ?? []

  return (
    <div className="doc-search-page">
      <h2>🔍 文档全文搜索</h2>
      <p className="hint">跨所有 story 的版本化文档（PRD/spec/plan/research/...）搜索最新内容。</p>

      <form
        className="doc-search-form"
        onSubmit={(e) => {
          e.preventDefault()
          setSubmitted(q.trim())
        }}
      >
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="搜索词（支持中文）"
          className="doc-search-input"
          autoFocus
        />
        <button className="btn btn-primary" type="submit" disabled={!q.trim()}>
          搜索
        </button>
      </form>

      {isLoading && <p>搜索中...</p>}

      {!isLoading && submitted && results.length === 0 && (
        <p className="hint">没有匹配的文档。</p>
      )}

      {results.length > 0 && (
        <div>
          <p className="hint">{results.length} 条结果</p>
          {results.map((r, i) => (
            <div
              key={`${r.story_key}-${r.doc_type}-${i}`}
              className="doc-search-result"
            >
              <div className="doc-search-result-head">
                <strong>
                  <Link to={`/story/${r.story_key}?tab=docs`}>
                    {r.story_key} · {r.doc_type}
                  </Link>
                </strong>
                <span className="hint">{r.title}</span>
              </div>
              <pre className="doc-search-snippet">{renderSnippet(r.snippet)}</pre>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
