// TAPD 状态/类型元数据 — TapdBoardPage 与 CalendarPage 共用(从 Dashboard 抽出)。
//
// 状态字典 2026-08-28 实测对齐 workspace 44381896 自定义工作流(印闪 SOP V1.7,
// workflows/status_map system=story + system=bug)。自定义状态是不透明 status_N,
// 展示优先用后端同步译好的 tapdStatusName;本表仅作兜底(老数据/后端未译)。

export const TAPD_STATUS: Record<string, string> = {
  // 需求流(前段:产品/设计/项管)
  status_17: '待规划',
  planning: '提需完成',
  status_30: '需求分析中',
  status_12: '需求准备就绪',
  status_11: '待评审',
  status_13: '评审不通过',
  status_36: '迭代评审完成',
  status_18: '待设计',
  status_32: '设计中',
  status_33: '设计完成',
  status_9: '待排期',
  // 需求流(开发段)
  status_2: '待开发',
  status_3: '开发中',
  status_35: '开发完成',
  // 需求流(测试段;status_40 冒烟测试通过为 SOP V1.7 新增)
  status_40: '冒烟测试通过',
  status_4: '待测试',
  status_5: '测试中',
  status_6: '待UAT测试',
  status_16: '验收不通过',
  // 需求流(上线/验收段)
  status_7: '待发布',
  status_8: '待产品验收',
  status_14: '产品验收中',
  status_15: '产品验收完成',
  status_37: '待业务验收',
  status_38: '业务验收中',
  status_39: '业务验收完成',
  // 需求流(终态/旁路)
  resolved: '已实现',
  rejected: '已拒绝',
  status_10: '已暂缓',
  // 子任务(任务类型三态)
  status_19: '未开始',
  status_20: '进行中',
  status_21: '已完成',
  // 缺陷(系统枚举)
  new: '新提交',
  in_progress: '处理中',
  reopened: '重新打开',
  suspended: '挂起',
  unconfirmed: '反馈',
  closed: '已关闭',
}

export const TYPE_LABELS: Record<string, { label: string; color: string }> = {
  story: { label: '需求', color: '#2563eb' },
  bug: { label: '缺陷', color: '#ef4444' },
  subtask: { label: '子任务', color: '#7c3aed' },
}

export const DONE_STATUSES = new Set(['resolved', 'rejected', 'closed', 'status_21'])
export const LOCAL_DONE_STATUSES = new Set(['completed', 'failed', 'aborted', 'archived'])

/** 展示用状态名:优先后端同步译名(tapdStatusName),兜底本地字典,最后原样。 */
export function tapdStatusLabel(story: { tapdStatusName?: string; tapdStatus?: string }): string {
  return story.tapdStatusName || TAPD_STATUS[story.tapdStatus || ''] || story.tapdStatus || ''
}
