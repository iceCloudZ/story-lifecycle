import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { docApi } from '../api/client'

/**
 * DocSearchPage — full-text search across all versioned docs (FTS5).
 * Results link into the story's docs tab.
 */
export default function DocSearchPage() {
  const [q, setQ] = useState('')
  const [submitted, setSubmitted] = useState('')

  const { data, isLoading } = useQuery({
    queryKey: ['doc-search', submitted],
    queryFn: () => docApi.search(submitted),
    enabled: !!submitted,
  })

  const results = data?.results ?? []

  return (
    <div style={{ padding: 24 }}>
      <h2>🔍 文档全文搜索</h2>
      <p className="hint">跨所有 story 的版本化文档（PRD/spec/plan/research/...）搜索最新内容。</p>

      <form
        onSubmit={(e) => {
          e.preventDefault()
          setSubmitted(q.trim())
        }}
        style={{ display: 'flex', gap: 8, margin: '12px 0' }}
      >
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="搜索词（支持中文）"
          style={{ flex: 1, maxWidth: 600 }}
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
              style={{
                padding: 12,
                marginBottom: 8,
                border: '1px solid #eee',
                borderRadius: 6,
              }}
            >
              <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                <strong>
                  <Link to={`/story/${r.story_key}?tab=docs`}>
                    {r.story_key} · {r.doc_type}
                  </Link>
                </strong>
                <span className="hint">{r.title}</span>
              </div>
              <pre
                style={{
                  fontSize: 13,
                  margin: '6px 0 0',
                  whiteSpace: 'pre-wrap',
                  background: '#fafafa',
                  padding: 8,
                  borderRadius: 4,
                }}
                dangerouslySetInnerHTML={{
                  // snippet from FTS5 already has [match] markers; render as text
                  __html: (r.snippet || '')
                    .replace(/&/g, '&amp;')
                    .replace(/</g, '&lt;')
                    .replace(/\[(.+?)\]/g, '<mark>$1</mark>'),
                }}
              />
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
