import { useQuery } from '@tanstack/react-query'
import { storyApi } from '../api/client'
import type { Finding } from '../api/client'

interface Props {
  storyKey: string
}

export default function TestTab({ storyKey }: Props) {
  const { data: findingsData } = useQuery({
    queryKey: ['findings', storyKey, 'test'],
    queryFn: () => storyApi.findings(storyKey),
    enabled: !!storyKey,
  })

  // In MVP, test data comes from findings with test-related categories.
  // When backend adds a dedicated test API, switch to that.
  const testFindings = (findingsData?.findings ?? []).filter(
    (f: Finding) => f.category === 'missing_test' || f.category === 'test' || f.source === 'test'
  )

  return (
    <div className="tab-content test-tab">
      {/* Stats bar */}
      <div className="tt-stats">
        <div className="tt-stat">
          <div className="tt-stat-num">--</div>
          <div className="tt-stat-label">用例总数</div>
        </div>
        <div className="tt-stat">
          <div className="tt-stat-num" style={{ color: '#3fb950' }}>--</div>
          <div className="tt-stat-label">通过</div>
        </div>
        <div className="tt-stat">
          <div className="tt-stat-num" style={{ color: '#f85149' }}>--</div>
          <div className="tt-stat-label">失败</div>
        </div>
        <div className="tt-stat">
          <div className="tt-stat-num" style={{ color: '#f0883e' }}>--</div>
          <div className="tt-stat-label">跳过</div>
        </div>
      </div>

      {/* Test case table */}
      {testFindings.length > 0 ? (
        <div className="tt-table">
          <div className="tt-table-header">
            <span>测试点</span>
            <span>覆盖范围</span>
            <span>状态</span>
          </div>
          {testFindings.map((f: Finding, i: number) => (
            <div key={i} className="tt-table-row">
              <span className="tt-test-point">{f.description || f.category}</span>
              <span className="tt-coverage">{f.location || '--'}</span>
              <span className={`tt-status tt-status-${f.status}`}>{f.status}</span>
            </div>
          ))}
        </div>
      ) : (
        <div className="tt-empty">
          暂无测试数据。测试阶段执行完成后会在这里展示测试用例和结果。
        </div>
      )}
    </div>
  )
}
