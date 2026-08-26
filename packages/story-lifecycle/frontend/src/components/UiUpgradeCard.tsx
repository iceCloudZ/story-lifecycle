/**
 * UiUpgradeCard — 上线/结项 终端门的 UI 确认卡(Q2)。
 *
 * 后端 advance/直设指向 上线|结项 时不再直接落位:写 _upgrade_gate 挂起并返回
 * 428(action=ui_confirm)。本卡片轮询 GET /lifecycle/pending,有挂起即展示确认/
 * 驳回;只有点击走 /lifecycle/ui-upgrade 才会真正落位——CLI/agent 的 428 断路在此闭环。
 * 边界(诚实):单机无认证域,理论可拼包绕过;配合 master No-one 平台层为纵深之一。
 */
import { useQuery, useQueryClient } from '@tanstack/react-query'

interface PendingGate {
  prev: string
  target: string
  origin: string
  requested_at: number
}

export function UiUpgradeCard({ storyKey }: { storyKey: string }) {
  const qc = useQueryClient()
  const { data } = useQuery({
    queryKey: ['ui-upgrade-pending', storyKey],
    queryFn: async () => {
      const r = await fetch(`/api/story/${storyKey}/lifecycle/pending`)
      if (!r.ok) return null
      const j = await r.json()
      return (j.pending ?? null) as PendingGate | null
    },
    refetchInterval: 30_000,
  })

  if (!data) return null

  async function act(action: 'confirm' | 'reject') {
    const r = await fetch(`/api/story/${storyKey}/lifecycle/ui-upgrade`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action }),
    })
    if (!r.ok) {
      alert(`操作失败: ${(await r.json()).detail || '未知错误'}`)
      return
    }
    qc.invalidateQueries({ queryKey: ['ui-upgrade-pending', storyKey] })
    qc.invalidateQueries({ queryKey: ['plan', storyKey] })
    qc.invalidateQueries({ queryKey: ['story', storyKey] })
  }

  return (
    <div className="ui-card" style={{ borderColor: '#d97706' }}>
      <div style={{ fontWeight: 600, marginBottom: 4 }}>
        ⚠️ 待人工确认:{data.prev} → <b>{data.target}</b>
      </div>
      <div style={{ fontSize: 12, color: '#6b7280', marginBottom: 8 }}>
        该跃迁涉及真实发布语义,请核对生产部署(Skyladder)与规范校验后再确认。
        来源: {data.origin}
      </div>
      <div className="ui-chip-row">
        <button type="button" className="ui-chip active" onClick={() => act('confirm')}>
          ✓ 确认{data.target}
        </button>
        <button type="button" className="ui-chip" onClick={() => act('reject')}>
          ✕ 驳回
        </button>
      </div>
    </div>
  )
}
