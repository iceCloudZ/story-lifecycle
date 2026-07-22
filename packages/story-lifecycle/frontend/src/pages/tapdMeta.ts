// TAPD 状态/类型元数据 — TapdBoardPage 与 CalendarPage 共用(从 Dashboard 抽出)。

export const TAPD_STATUS: Record<string, string> = {
  status_2: '待开发',
  status_3: '开发中',
  status_4: '待测试',
  status_5: '测试中',
  status_7: '待发布',
  status_8: '待产品验收',
  status_9: '待排期',
  status_11: '待评审',
  status_17: '待规划',
  status_18: '待设计',
  status_19: '未开始',
  status_20: '进行中',
  status_21: '已完成',
  status_32: '设计中',
  status_37: '待业务验收',
  resolved: '已实现',
  closed: '已关闭',
  rejected: '已拒绝',
}

export const TYPE_LABELS: Record<string, { label: string; color: string }> = {
  story: { label: '需求', color: '#2563eb' },
  bug: { label: '缺陷', color: '#ef4444' },
  subtask: { label: '子任务', color: '#7c3aed' },
}

export const DONE_STATUSES = new Set(['resolved', 'rejected', 'closed', 'status_21'])
export const LOCAL_DONE_STATUSES = new Set(['completed', 'failed', 'aborted', 'archived'])
