import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { EditorState } from '@codemirror/state'
import { EditorView, basicSetup } from 'codemirror'
import { markdown } from '@codemirror/lang-markdown'
import ReactDiffViewer, { DiffMethod } from 'react-diff-viewer-continued'
import { Diff as RDiffView, Hunk as RHunk, parseDiff as rParseDiff } from 'react-diff-view'
import 'react-diff-view/style/index.css'
import { docApi } from '../api/client'
import MarkdownView from './MarkdownView'
import './DocEditor.css'

interface Props {
  storyKey: string
  docType: string
  onBack: () => void
}

/**
 * DocEditor — 版本化文档的预览 + 编辑。
 *
 * 默认 view 模式(纯预览,轻量不挂 CodeMirror);点「编辑」进 edit 模式
 * (左右分屏:左 CodeMirror 编辑 / 右 MarkdownView 实时预览)。保存后退回预览。
 * 右侧版本历史两种模式都可见(diff / 回滚)。
 */
export default function DocEditor({ storyKey, docType, onBack }: Props) {
  const qc = useQueryClient()
  const editorRef = useRef<HTMLDivElement>(null)
  const viewRef = useRef<EditorView | null>(null)
  const [mode, setMode] = useState<'view' | 'edit'>('view')
  const [content, setContent] = useState('')
  const [reason, setReason] = useState('')
  const [title, setTitle] = useState('')
  const [saving, setSaving] = useState(false)
  // diff 弹层:两个 UI 库并排对比(react-diff-view vs react-diff-viewer-continued),
  // 都用 split 视图,让用户选保留哪个。两份数据:patch(库A 要)+ 原文(库B 要)。
  const [showDiff, setShowDiff] = useState<{
    a: number
    b: number
    patch: string           // 后端 unified patch(库A react-diff-view 用)
    oldContent: string      // 原文(库B react-diff-viewer-continued 用)
    newContent: string
    which: 'libA' | 'libB'  // 当前看哪个库
  } | null>(null)
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

  // 进入组件 / 文档切换时加载内容
  useEffect(() => {
    if (doc?.latest_content !== undefined && !isLoading) {
      setContent(doc.latest_content)
      setTitle(doc.title || '')
    }
  }, [doc, isLoading])

  // 进入 edit 模式时挂载 CodeMirror(延迟到需要时才创建,view 模式不加载)
  useEffect(() => {
    if (mode !== 'edit') return
    if (!editorRef.current) return
    if (viewRef.current) return // 已挂载
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
  }, [mode])

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
      setMode('view') // 保存成功退回预览
    } catch (e) {
      alert(`保存失败: ${e instanceof Error ? e.message : e}`)
    } finally {
      setSaving(false)
    }
  }

  const viewDiff = async (a: number, b: number) => {
    try {
      // 同时拉:patch(库A react-diff-view 要)+ 两版原文(库B 要)。
      const [diffResp, va, vb] = await Promise.all([
        docApi.diff(storyKey, docType, a, b),
        docApi.getVersion(storyKey, docType, a),
        docApi.getVersion(storyKey, docType, b),
      ])
      setShowDiff({
        a,
        b,
        patch: diffResp.diff,
        oldContent: va.content,
        newContent: vb.content,
        which: 'libB', // 默认库B(react-diff-viewer-continued),高亮更开箱即用
      })
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
    <div className="doc-editor">
      {/* 顶栏 */}
      <div className="doc-editor-topbar">
        <button className="btn btn-back" onClick={onBack}>← 返回</button>
        <strong className="doc-editor-doctype">{docType}</strong>
        {mode === 'view' ? (
          <>
            <span className="doc-editor-title-display">{title}</span>
            <button className="btn btn-primary" onClick={() => setMode('edit')}>编辑</button>
          </>
        ) : (
          <>
            <input
              className="doc-editor-title-input"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="标题"
            />
            <button className="btn" onClick={() => setMode('view')}>预览</button>
          </>
        )}
        <span className="hint">v{doc?.current_version ?? '?'}</span>
      </div>

      <div className="doc-editor-body">
        {/* 主区:view=纯预览 / edit=左右分屏 */}
        <div className="doc-editor-main">
          {mode === 'view' ? (
            <div className="doc-editor-preview-pane">
              {isLoading ? (
                <p className="hint" style={{ padding: 12 }}>加载中...</p>
              ) : content ? (
                <MarkdownView content={content} />
              ) : (
                <p className="hint">（空文档）</p>
              )}
            </div>
          ) : (
            <>
              <div className="doc-editor-edit-col">
                <div className="doc-editor-codemirror-wrap">
                  <div ref={editorRef} style={{ height: '100%' }} />
                </div>
                <div className="doc-editor-save-bar">
                  <input
                    value={reason}
                    onChange={(e) => setReason(e.target.value)}
                    placeholder="修改理由（必填）"
                    className="doc-editor-reason-input"
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
              <div className="doc-editor-preview-col">
                <MarkdownView content={content} />
              </div>
            </>
          )}
        </div>

        {/* 右:版本历史(两种模式都保留) */}
        <div className="doc-editor-versions">
          <h4>版本历史</h4>
          {versions.length === 0 ? (
            <p className="hint">暂无历史</p>
          ) : (
            versions.map((v) => (
              <div key={v.version} className="doc-version-item">
                <div className="doc-version-head">
                  <strong>v{v.version}</strong>
                  <span className={`doc-author-badge${v.author === 'ai' ? ' ai' : ''}`}>
                    {v.author}
                  </span>
                </div>
                <div className="hint doc-version-time">{v.created_at}</div>
                <div className="doc-version-reason">{v.change_reason}</div>
                <div className="doc-version-actions">
                  {doc && v.version !== doc.current_version && (
                    <button
                      className="btn btn-sm"
                      onClick={() => viewDiff(v.version, doc.current_version)}
                    >
                      diff v{v.version}→v{doc.current_version}
                    </button>
                  )}
                  <button className="btn btn-sm" onClick={() => setRollbackTarget(v.version)}>
                    回滚
                  </button>
                </div>
              </div>
            ))
          )}
        </div>
      </div>

      {/* diff 弹层:两个 UI 库对比 —— react-diff-view(库A) vs react-diff-viewer-continued(库B)。
          两个都渲染 split(左右并排),用 tab 切换,让你直观比较后选保留哪个。 */}
      {showDiff && (
        <div className="doc-editor-modal doc-diff-modal">
          <div className="doc-modal-head">
            <strong>diff v{showDiff.a} → v{showDiff.b}</strong>
            <div className="doc-diff-lib-toggle">
              <button
                type="button"
                className={`btn btn-sm${showDiff.which === 'libA' ? ' btn-primary' : ''}`}
                onClick={() => setShowDiff({ ...showDiff, which: 'libA' })}
                title="react-diff-view(轻量,渲染 git patch hunk)"
              >
                库A: react-diff-view
              </button>
              <button
                type="button"
                className={`btn btn-sm${showDiff.which === 'libB' ? ' btn-primary' : ''}`}
                onClick={() => setShowDiff({ ...showDiff, which: 'libB' })}
                title="react-diff-viewer-continued(自带词级高亮 + 行号)"
              >
                库B: react-diff-viewer-continued
              </button>
            </div>
            <button className="btn btn-sm" onClick={() => setShowDiff(null)}>关闭</button>
          </div>
          <p className="hint doc-diff-compare-hint">
            两个库都用 split(左右并排)视图。比较后告诉我保留哪个,我会删掉另一个库 + 这个切换器。
          </p>
          <div className="doc-diff-viewer-wrap">
            {showDiff.which === 'libA' ? (
              (() => {
                const files = rParseDiff(showDiff.patch || '')
                if (!files.length) {
                  return <div className="doc-diff-empty">（无 diff 内容 / patch 解析为空）</div>
                }
                return files.map((file) => (
                  <RDiffView
                    key={file.newPath || file.oldPath || 'f'}
                    viewType="split"
                    diffType={file.type}
                    hunks={file.hunks}
                  >
                    {(hunks) => hunks.map((hunk) => <RHunk key={hunk.content} hunk={hunk} />)}
                  </RDiffView>
                ))
              })()
            ) : (
              <ReactDiffViewer
                oldValue={showDiff.oldContent}
                newValue={showDiff.newContent}
                splitView
                compareMethod={DiffMethod.WORDS}
                useDarkTheme={false}
                // 把每行 markdown 渲染成格式化内容(标题/粗体/列表…),而非裸源码。
                // 行级 +/- 背景高亮由行容器管,renderContent 只替换内容,高亮不丢。
                // 注:逐行渲染,跨行结构(多行表格/代码块)可能不完整 —— 文档以行为主时够用。
                renderContent={(source: string) => (
                  <span className="doc-diff-md-line">
                    <MarkdownView content={source} />
                  </span>
                )}
                styles={{
                  // 统一字号(两个库默认字号不同,这里强制对齐项目 text-sm)。
                  contentText: { fontSize: 'var(--text-sm)' },
                  diffRemoved: { fontSize: 'var(--text-sm)' },
                  diffAdded: { fontSize: 'var(--text-sm)' },
                }}
              />
            )}
          </div>
        </div>
      )}

      {/* 回滚确认 */}
      {rollbackTarget !== null && (
        <div className="doc-editor-modal doc-rollback-modal">
          <strong>回滚到 v{rollbackTarget}?</strong>
          <p className="hint">（会用 v{rollbackTarget} 的内容创建一个新版本，历史保留）</p>
          <input
            value={rollbackReason}
            onChange={(e) => setRollbackReason(e.target.value)}
            placeholder="回滚理由（必填）"
            className="doc-editor-reason-input full"
          />
          <div className="doc-modal-actions">
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
