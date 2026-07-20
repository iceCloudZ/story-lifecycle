import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { EditorState } from '@codemirror/state'
import { EditorView, basicSetup } from 'codemirror'
import { markdown } from '@codemirror/lang-markdown'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { docApi } from '../api/client'

interface Props {
  storyKey: string
  docType: string
  onBack: () => void
}

/**
 * DocEditor — edit a versioned doc with CodeMirror + live preview.
 * Save requires a change-reason. Right rail shows version history (AI/User
 * tagged) with diff + rollback.
 */
export default function DocEditor({ storyKey, docType, onBack }: Props) {
  const qc = useQueryClient()
  const editorRef = useRef<HTMLDivElement>(null)
  const viewRef = useRef<EditorView | null>(null)
  const [content, setContent] = useState('')
  const [reason, setReason] = useState('')
  const [title, setTitle] = useState('')
  const [saving, setSaving] = useState(false)
  const [showDiff, setShowDiff] = useState<{ a: number; b: number; diff: string } | null>(null)
  const [rollbackTarget, setRollbackTarget] = useState<number | null>(null)
  const [rollbackReason, setRollbackReason] = useState('')

  // Load latest content
  const { data: doc, isLoading } = useQuery({
    queryKey: ['doc', storyKey, docType],
    queryFn: () => docApi.getDoc(storyKey, docType),
    enabled: !!storyKey && !!docType,
  })

  // Load version history
  const { data: versionsData } = useQuery({
    queryKey: ['doc-versions', storyKey, docType],
    queryFn: () => docApi.listVersions(storyKey, docType),
    enabled: !!storyKey && !!docType,
  })

  // Init doc content into editor when loaded
  useEffect(() => {
    if (doc?.latest_content !== undefined && content === '' && !isLoading) {
      setContent(doc.latest_content)
      setTitle(doc.title || '')
    }
  }, [doc, content, isLoading])

  // Set up CodeMirror once
  useEffect(() => {
    if (!editorRef.current) return
    if (viewRef.current) return // already set up
    const state = EditorState.create({
      doc: content,
      extensions: [
        basicSetup,
        markdown(),
        EditorView.lineWrapping,
        EditorView.updateListener.of((u) => {
          if (u.docChanged) setContent(u.state.doc.toString())
        }),
      ],
    })
    viewRef.current = new EditorView({ state, parent: editorRef.current })
    return () => {
      viewRef.current?.destroy()
      viewRef.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [content === '']) // only init once when content first arrives

  const versions = versionsData?.versions ?? []

  const save = async () => {
    if (!reason.trim()) {
      alert('请填写修改理由（必填）')
      return
    }
    setSaving(true)
    try {
      await docApi.saveDoc(storyKey, docType, content, reason.trim(), title)
      setReason('')
      qc.invalidateQueries({ queryKey: ['doc', storyKey, docType] })
      qc.invalidateQueries({ queryKey: ['doc-versions', storyKey, docType] })
    } catch (e) {
      alert(`保存失败: ${e instanceof Error ? e.message : e}`)
    } finally {
      setSaving(false)
    }
  }

  const viewDiff = async (a: number, b: number) => {
    try {
      const r = await docApi.diff(storyKey, docType, a, b)
      setShowDiff({ a, b, diff: r.diff })
    } catch (e) {
      alert(`diff 失败: ${e instanceof Error ? e.message : e}`)
    }
  }

  const doRollback = async () => {
    if (rollbackTarget === null || !rollbackReason.trim()) return
    try {
      await docApi.rollback(storyKey, docType, rollbackTarget, rollbackReason.trim())
      setRollbackTarget(null)
      setRollbackReason('')
      qc.invalidateQueries({ queryKey: ['doc', storyKey, docType] })
      qc.invalidateQueries({ queryKey: ['doc-versions', storyKey, docType] })
    } catch (e) {
      alert(`回滚失败: ${e instanceof Error ? e.message : e}`)
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      {/* 顶栏: 返回 + 标题 */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 12 }}>
        <button className="btn btn-back" onClick={onBack}>← 返回</button>
        <strong style={{ fontSize: 16 }}>{docType}</strong>
        <input
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="标题"
          style={{ flex: 1 }}
        />
        <span className="hint">v{doc?.current_version ?? '?'}</span>
      </div>

      <div style={{ display: 'flex', gap: 16, flex: 1, minHeight: 0 }}>
        {/* 左:编辑器 + 右:预览 */}
        <div style={{ flex: 2, display: 'flex', flexDirection: 'column', minWidth: 0 }}>
          <div style={{ flex: 1, border: '1px solid #ddd', overflow: 'auto' }}>
            {isLoading ? (
              <p style={{ padding: 12 }}>加载中...</p>
            ) : (
              <div ref={editorRef} style={{ height: '100%' }} />
            )}
          </div>
          {/* 预览 */}
          <details style={{ marginTop: 8 }}>
            <summary style={{ cursor: 'pointer' }}>预览</summary>
            <div
              className="markdown-preview"
              style={{ padding: 12, border: '1px solid #eee', maxHeight: 300, overflow: 'auto' }}
            >
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
            </div>
          </details>
          {/* 保存栏 */}
          <div style={{ display: 'flex', gap: 8, marginTop: 8, alignItems: 'center' }}>
            <input
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="修改理由（必填）"
              style={{ flex: 1 }}
            />
            <button
              className="btn btn-primary"
              onClick={save}
              disabled={saving || !reason.trim()}
            >
              {saving ? '保存中...' : '保存新版本'}
            </button>
          </div>
        </div>

        {/* 右:版本历史 */}
        <div style={{ width: 280, border: '1px solid #ddd', padding: 8, overflow: 'auto' }}>
          <h4 style={{ margin: '0 0 8px' }}>版本历史</h4>
          {versions.length === 0 ? (
            <p className="hint">暂无历史</p>
          ) : (
            versions.map((v) => (
              <div
                key={v.version}
                style={{
                  padding: 6,
                  marginBottom: 4,
                  border: '1px solid #eee',
                  borderRadius: 4,
                  fontSize: 13,
                }}
              >
                <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                  <strong>v{v.version}</strong>
                  <span
                    style={{
                      fontSize: 11,
                      padding: '1px 6px',
                      borderRadius: 8,
                      background: v.author === 'ai' ? '#eef' : '#efe',
                    }}
                  >
                    {v.author}
                  </span>
                </div>
                <div className="hint" style={{ margin: '2px 0' }}>{v.created_at}</div>
                <div style={{ color: '#555' }}>{v.change_reason}</div>
                <div style={{ display: 'flex', gap: 4, marginTop: 4 }}>
                  {doc && v.version !== doc.current_version && (
                    <button
                      className="btn btn-sm"
                      onClick={() => viewDiff(v.version, doc.current_version)}
                    >
                      diff v{v.version}→v{doc.current_version}
                    </button>
                  )}
                  <button
                    className="btn btn-sm"
                    onClick={() => setRollbackTarget(v.version)}
                  >
                    回滚
                  </button>
                </div>
              </div>
            ))
          )}
        </div>
      </div>

      {/* diff 弹层 */}
      {showDiff && (
        <div
          style={{
            position: 'fixed',
            top: '10%',
            left: '10%',
            right: '10%',
            bottom: '10%',
            background: 'white',
            border: '1px solid #888',
            boxShadow: '0 0 20px rgba(0,0,0,0.3)',
            zIndex: 1000,
            padding: 16,
            overflow: 'auto',
          }}
        >
          <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
            <strong>diff v{showDiff.a} → v{showDiff.b}</strong>
            <button className="btn btn-sm" onClick={() => setShowDiff(null)}>关闭</button>
          </div>
          <pre style={{ fontSize: 13, whiteSpace: 'pre-wrap' }}>{showDiff.diff}</pre>
        </div>
      )}

      {/* 回滚确认 */}
      {rollbackTarget !== null && (
        <div
          style={{
            position: 'fixed',
            top: '30%',
            left: '30%',
            right: '30%',
            background: 'white',
            border: '1px solid #888',
            padding: 16,
            zIndex: 1000,
          }}
        >
          <strong>回滚到 v{rollbackTarget}?</strong>
          <p className="hint">（会用 v{rollbackTarget} 的内容创建一个新版本，历史保留）</p>
          <input
            value={rollbackReason}
            onChange={(e) => setRollbackReason(e.target.value)}
            placeholder="回滚理由（必填）"
            style={{ width: '100%', margin: '8px 0' }}
          />
          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
            <button
              className="btn"
              onClick={() => {
                setRollbackTarget(null)
                setRollbackReason('')
              }}
            >
              取消
            </button>
            <button
              className="btn btn-primary"
              onClick={doRollback}
              disabled={!rollbackReason.trim()}
            >
              确认回滚
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
