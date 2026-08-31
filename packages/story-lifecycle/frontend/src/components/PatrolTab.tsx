import { useQuery } from '@tanstack/react-query'
import { patrolApi } from '../api/client'
import type { PatrolItem, PatrolRun, PatrolRunItemResult } from '../api/client'
import './PatrolTab.css'

/**
 * 生产巡检 tab（docs/design-prod-patrol-integration.md §4.3）。
 *
 * 巡检发生在 story 上线后的观察期,由外部 prod-patrol skill 触发与回写
 * (PUT items / POST runs),本 tab 只读展示:巡检项清单 + 轮次历史时间线。
 * 展示模式复用 QualityPanel(ui-card + ui-list)。
 */

// 逐项/轮次结论徽标:PASS 绿 / FAIL 红 / WAIVED 琥珀 / SKIP 灰(复用全局 badge 变体)
function ResultBadge({ result }: { result: string }) {
  const cls =
    result === 'PASS'
      ? 'badge-active'
      : result === 'FAIL'
        ? 'badge-failed'
        : result === 'WAIVED'
          ? 'badge-paused'
          : 'badge-aborted'
  return <span className={`badge ${cls} pt-res-badge`}>{result}</span>
}

function fmtTime(ts: string): string {
  // startedAt 是 "YYYY-MM-DD HH:MM:SS",列表里只展示月日+时分
  return ts.length >= 16 ? ts.slice(5, 16) : ts
}

function ItemRow({ item }: { item: PatrolItem }) {
  const paramsJson = Object.keys(item.params).length > 0 ? JSON.stringify(item.params) : ''
  return (
    <li className={`ui-list-row pt-item ${item.enabled ? '' : 'pt-item-disabled'}`} data-testid="pt-item">
      <div className="pt-item-main">
        <span className="pt-item-seq">{item.seq}</span>
        <span className="pt-item-name">{item.name}</span>
        <span className="pt-item-type">{item.type}</span>
        {!item.enabled && <span className="badge badge-aborted">停用</span>}
      </div>
      <div className="pt-item-meta">
        {item.passCriteria && (
          <span className="pt-item-criterion">
            判定<span className="pt-item-label-sep">：</span>
            {item.passCriteria}
          </span>
        )}
        {item.baseline && <span className="pt-item-baseline">基线 {item.baseline}</span>}
        {item.rollbackRef && <span className="pt-item-rollback">回滚 {item.rollbackRef}</span>}
      </div>
      {paramsJson && <div className="pt-item-params">{paramsJson}</div>}
    </li>
  )
}

function RunItemRow({ item }: { item: PatrolRunItemResult }) {
  return (
    <li className="ui-list-row pt-run-item" data-testid="pt-run-item">
      <span className="pt-run-item-seq">{item.seq}</span>
      <span className="pt-run-item-name">{item.name ?? `#${item.seq}`}</span>
      <ResultBadge result={item.result} />
      {item.observed && <span className="pt-run-item-observed">{item.observed}</span>}
      {item.evidenceRef && <span className="pt-run-item-evidence">{item.evidenceRef}</span>}
    </li>
  )
}

function RunBlock({ run }: { run: PatrolRun }) {
  return (
    <div className={`pt-run ${run.result === 'FAIL' ? 'pt-run-failed' : ''}`} data-testid="pt-run">
      <div className="pt-run-head">
        <ResultBadge result={run.result} />
        <span className="pt-run-time">{fmtTime(run.startedAt)}</span>
        {run.executor && <span className="pt-run-executor">{run.executor}</span>}
        {run.runScope && <span className="pt-run-scope">{run.runScope}</span>}
      </div>
      {run.summary && <div className="pt-run-summary">{run.summary}</div>}
      {run.items.length > 0 && (
        <ul className="ui-list pt-run-items" data-testid="pt-run-items">
          {run.items.map((it, i) => (
            <RunItemRow key={i} item={it} />
          ))}
        </ul>
      )}
    </div>
  )
}

export default function PatrolTab({ storyKey }: { storyKey: string }) {
  const itemsQuery = useQuery({
    queryKey: ['patrol-items', storyKey],
    queryFn: () => patrolApi.items(storyKey),
    refetchInterval: 15000,
  })
  const runsQuery = useQuery({
    queryKey: ['patrol-runs', storyKey],
    queryFn: () => patrolApi.runs(storyKey),
    refetchInterval: 15000,
  })

  const items: PatrolItem[] = itemsQuery.data?.items ?? []
  const runs: PatrolRun[] = runsQuery.data?.runs ?? []

  return (
    <div className="ui-card pt-panel" data-testid="pt-panel">
      <h3 className="ui-section-title pt-title">生产巡检</h3>

      <div className="pt-section">
        <h4 className="ui-section-title pt-sub">
          巡检项（{items.length}）
        </h4>
        {itemsQuery.isLoading ? (
          <p className="ui-hint">加载中…</p>
        ) : items.length === 0 ? (
          <p className="ui-hint" data-testid="pt-items-empty">
            尚未登记巡检项——需求上线后由 prod-patrol skill 登记或调 PUT patrol/items 接口
          </p>
        ) : (
          <ul className="ui-list pt-items" data-testid="pt-items">
            {items.map((it) => (
              <ItemRow key={it.seq} item={it} />
            ))}
          </ul>
        )}
      </div>

      <div className="pt-section">
        <h4 className="ui-section-title pt-sub">巡检轮次</h4>
        {runsQuery.isLoading ? (
          <p className="ui-hint">加载中…</p>
        ) : runs.length === 0 ? (
          <p className="ui-hint" data-testid="pt-runs-empty">
            尚无巡检轮次记录
          </p>
        ) : (
          <div className="pt-runs" data-testid="pt-runs">
            {runs.map((r) => (
              <RunBlock key={r.id} run={r} />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
