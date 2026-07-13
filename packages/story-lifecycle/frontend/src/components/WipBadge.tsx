import './WipBadge.css'

interface WipBadgeProps {
  count: number
  limit?: number
}

export default function WipBadge({ count, limit = 3 }: WipBadgeProps) {
  const over = count > limit
  return (
    <span className={`wip-badge ${over ? 'wip-over' : ''}`} title={`WIP 限制: ${limit}`}>
      WIP: {count}
      {over && <span className="wip-warning"> ⚠️</span>}
    </span>
  )
}
