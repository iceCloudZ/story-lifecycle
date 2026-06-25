import { useMemo, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import './BugsPage.css'

interface BugSummary {
  storyKey: string
  title?: string
  status?: string
  tapdStatus?: string
  priority?: string
  owner?: string
  deadline?: string
  tapdUrl?: string
  updatedAt?: string
  parentKey?: string
}

const statusLabel: Record<string, string> = {
  idle: '未开始',
  planning: '规划中',
  active: '开发中',
  paused: '已暂停',
  blocked: '阻塞中',
  completed: '已完成',
  failed: '失败',
  aborted: '已终止',
  archived: '已归档',
}

const priClass: Record<string, string> = {
  urgent: 'bug-pri-urgent',
  high: 'bug-pri-high',
  medium: 'bug-pri-medium',
  low: 'bug-pri-low',
}

const priLabel: Record<string, string> = {
  urgent: '紧急',
  high: '高',
  medium: '中',
  low: '低',
}

export default function BugsPage() {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [showAll, setShowAll] = useState(false)
  const [keyword, setKeyword] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [priFilter, setPriFilter] = useState('')
  const [ownerFilter, setOwnerFilter] = useState('')
  const [syncing, setSyncing] = useState(false)

  const { data: bugs, isLoading, error } = useQuery<BugSummary[]>({
    queryKey: ['bugs', showAll],
    queryFn: async () => {
      const r = await fetch(`/api/bugs?show_all=${showAll}`)
      if (!r.ok) throw new Error('load bugs failed')
      return r.json()
    },
  })

  const owners = useMemo(() => {
    if (!bugs) return []
    const set = new Set<string>()
    bugs.forEach((b) => {
      const o = (b.owner || '').replace(/;$/, '')
      if (o) set.add(o)
    })
    return Array.from(set).sort()
  }, [bugs])

  const filtered = useMemo(() => {
    if (!bugs) return []
    const kw = keyword.trim().toLowerCase()
    return bugs.filter((b) => {
      if (kw && !(b.storyKey.toLowerCase().includes(kw) || (b.title || '').toLowerCase().includes(kw))) return false
      if (statusFilter && b.status !== statusFilter) return false
      if (priFilter && b.priority !== priFilter) return false
      if (ownerFilter && !(b.owner || '').includes(ownerFilter)) return false
      return true
    })
  }, [bugs, keyword, statusFilter, priFilter, ownerFilter])

  async function syncAllBugs() {
    setSyncing(true)
    try {
      const r = await fetch('/api/sync/tapd', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ item_type: 'bug' }),
      })
      if (!r.ok) throw new Error('sync bugs failed')
      await r.json()
      qc.invalidateQueries({ queryKey: ['bugs'] })
    } catch (e) {
      alert((e as Error).message)
    } finally {
      setSyncing(false)
    }
  }

  const openCount = bugs?.filter((b) => b.status !== 'completed' && b.status !== 'archived' && b.status !== 'aborted').length ?? 0

  if (isLoading) return <div className="bugs-page">加载中...</div>
  if (error) return <div className="bugs-page">加载失败：{(error as Error).message}</div>

  return (
    <div className="bugs-page">
      <div className="bugs-header">
        <div>
          <h2>缺陷列表</h2>
          <div className="bugs-meta">
            共 {bugs?.length ?? 0} 条，未关闭 {openCount} 条，当前筛选 {filtered.length} 条
          </div>
        </div>
        <div className="bugs-actions">
          <label className="toggle">
            <input type="checkbox" checked={showAll} onChange={(e) => setShowAll(e.target.checked)} />
            显示已关闭
          </label>
          <button className="btn btn-primary" onClick={syncAllBugs} disabled={syncing}>
            {syncing ? '同步中...' : '同步 TAPD 缺陷'}
          </button>
        </div>
      </div>

      <div className="bugs-filters">
        <input
          className="filter-input"
          placeholder="搜索 key / 标题"
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
        />
        <select className="filter-select" value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
          <option value="">所有本地状态</option>
          {Object.entries(statusLabel).map(([k, v]) => (
            <option key={k} value={k}>{v}</option>
          ))}
        </select>
        <select className="filter-select" value={priFilter} onChange={(e) => setPriFilter(e.target.value)}>
          <option value="">所有优先级</option>
          {Object.entries(priLabel).map(([k, v]) => (
            <option key={k} value={k}>{v}</option>
          ))}
        </select>
        <select className="filter-select" value={ownerFilter} onChange={(e) => setOwnerFilter(e.target.value)}>
          <option value="">所有负责人</option>
          {owners.map((o) => (
            <option key={o} value={o}>{o}</option>
          ))}
        </select>
        <button className="btn" onClick={() => { setKeyword(''); setStatusFilter(''); setPriFilter(''); setOwnerFilter('') }}>
          重置
        </button>
      </div>

      {!filtered || filtered.length === 0 ? (
        <div className="empty-state">
          没有符合条件的缺陷
          <div className="hint">可调整筛选条件或点击右上角同步</div>
        </div>
      ) : (
        <div className="bug-table">
          <div className="bug-row bug-row-head">
            <span className="col-pri">优先级</span>
            <span className="col-title">标题</span>
            <span className="col-status">本地状态</span>
            <span className="col-tapd">TAPD 状态</span>
            <span className="col-owner">负责人</span>
            <span className="col-deadline">截止</span>
          </div>
          {filtered.map((bug) => (
            <div
              key={bug.storyKey}
              className="bug-row"
              onClick={() =>
                bug.parentKey
                  ? navigate(`/story/${bug.parentKey}?tab=bugs`)
                  : navigate(`/story/${bug.storyKey}?tab=overview`)
              }
            >
              <span className="col-pri">
                <span className={`bug-pri ${priClass[bug.priority ?? ''] || ''}`}>
                  {priLabel[bug.priority ?? ''] || bug.priority || '-'}
                </span>
              </span>
              <span className="col-title">
                <span className="bug-table-key">{bug.storyKey}</span>
                {bug.title || '(无标题)'}
              </span>
              <span className="col-status">{statusLabel[bug.status ?? ''] || bug.status}</span>
              <span className="col-tapd">{bug.tapdStatus || '-'}</span>
              <span className="col-owner">{bug.owner ? bug.owner.replace(/;$/, '') : '-'}</span>
              <span className="col-deadline">{bug.deadline ? bug.deadline.slice(0, 10) : '-'}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
