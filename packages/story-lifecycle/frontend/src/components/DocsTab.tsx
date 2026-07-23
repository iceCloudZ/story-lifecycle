import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { docApi, type DocListItem } from '../api/client'
import DocEditor from './DocEditor'
import './DocsTab.css'

/**
 * DocsTab — 版本化文档列表 + 进入预览/编辑。
 *
 * 点列表项进 DocEditor(默认预览模式)。新建文档后直接进编辑。
 */
export default function DocsTab({ storyKey }: { storyKey: string }) {
  const qc = useQueryClient()
  const [editing, setEditing] = useState<string | null>(null) // doc_type being viewed
  const [newType, setNewType] = useState('')
  const [newTitle, setNewTitle] = useState('')

  const { data, isLoading } = useQuery({
    queryKey: ['docs', storyKey],
    queryFn: () => docApi.list(storyKey),
    enabled: !!storyKey,
  })

  if (editing) {
    return (
      <DocEditor
        storyKey={storyKey}
        docType={editing}
        onBack={() => {
          setEditing(null)
          qc.invalidateQueries({ queryKey: ['docs', storyKey] })
        }}
      />
    )
  }

  const docs: DocListItem[] = data?.docs ?? []

  const createDoc = async () => {
    const t = newType.trim()
    if (!t) return
    // create v1 with stub content (user edits next)
    try {
      await docApi.saveDoc(storyKey, t, `# ${newTitle || t}\n\n(待编辑)\n`, '新建文档', newTitle)
      setNewType('')
      setNewTitle('')
      qc.invalidateQueries({ queryKey: ['docs', storyKey] })
      setEditing(t)
    } catch (e) {
      alert(`新建失败: ${e instanceof Error ? e.message : e}`)
    }
  }

  // 从本地 .md 同步到 DB:半自动 agent 可能直接改了文件没回写 DB,点这个把改动拉进来。
  async function syncDoc(docType: string) {
    try {
      const r = await docApi.syncFromLocal(storyKey, docType)
      if (r.synced) {
        alert(`已同步 ${docType} → v${r.version}(从本地文件拉取了改动)`)
      } else {
        alert(`${docType} 无需同步(文件与 DB 一致)`)
      }
      qc.invalidateQueries({ queryKey: ['docs', storyKey] })
    } catch (e) {
      alert(`同步失败: ${e instanceof Error ? e.message : e}`)
    }
  }

  return (
    <div className="docs-tab">
      <h3 className="docs-tab-title">📄 业务文档（版本化）</h3>
      <p className="docs-tab-hint">
        所有业务文档的服务器版本管理。DB 是唯一真相，本地 .md 是只读缓存。
        每次保存必须填修改理由；支持版本历史 / diff / 回滚 / 全文搜索。
      </p>

      {/* 新建文档 */}
      <div className="docs-create-row">
        <input
          className="docs-create-input"
          placeholder="文档类型（如 prd / spec / 会议纪要）"
          value={newType}
          onChange={(e) => setNewType(e.target.value)}
        />
        <input
          className="docs-create-input"
          placeholder="标题（可选）"
          value={newTitle}
          onChange={(e) => setNewTitle(e.target.value)}
        />
        <button className="btn btn-sm btn-primary" onClick={createDoc} disabled={!newType.trim()}>
          新建
        </button>
      </div>

      {/* 文档列表 */}
      {isLoading ? (
        <p className="hint">加载中...</p>
      ) : docs.length === 0 ? (
        <p className="hint">还没有版本化文档。在上方新建一个，或在 intake 创建 PRD 后它会自动出现。</p>
      ) : (
        <div className="docs-list">
          {docs.map((d) => (
            <div key={d.doc_type} className="docs-list-item" onClick={() => setEditing(d.doc_type)}>
              <div className="docs-item-info">
                <div className="docs-item-name">
                  <strong>{d.doc_type}</strong>
                  {d.title ? <span className="hint"> · {d.title}</span> : null}
                  {d.confirmed_by && <span className="docs-confirmed-badge">✓ 已确认</span>}
                </div>
                <div className="hint">
                  v{d.current_version} · {d.updated_by || '?'} · {d.updated_at}
                </div>
              </div>
              <button
                type="button"
                className="btn btn-sm docs-sync-btn"
                onClick={(e) => {
                  e.stopPropagation()
                  syncDoc(d.doc_type)
                }}
                title="把本地 .md 的改动同步到 DB(半自动 agent 直接改文件时用)"
              >
                从文件同步
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
