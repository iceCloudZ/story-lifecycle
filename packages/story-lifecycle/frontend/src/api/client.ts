const BASE = ''

// ---- Domain types for the orchestrator REST API ----------------------------
// The backend returns dict shapes from raw SQL (no Pydantic models), so these
// mirror the fields the frontend actually consumes. Optional fields are ones
// the backend may omit or send as null depending on source/progress.

export interface Story {
  storyKey: string
  title: string
  currentStage: string
  status: string
  complexity?: string | null
  workspace?: string
  profile: string
  executionCount: number
  updatedAt: string
  deadline?: string
  priority?: string
  owner?: string
  tapdStatus?: string
  tapdUrl?: string
  tapdType?: string
  intakeState?: string | null
  sourceType?: string
  sourceId?: string
  lastError?: string
  // Aggregated findings counts (present in some list responses)
  findingsCount?: number
  openFindings?: number
  highSeverityFindings?: number
  // STORY-STATE-MODEL: Story 业务状态(开发/测试/上线),独立第一公民
  lifecycleState?: string | null
  // 班车看板:story 归属班车(v3.2/v3.3/后台快线/...),NULL=待分配
  releaseTrain?: string | null
  // 状态治理:测试/demo story 标记,看板默认过滤掉
  isTest?: boolean | null
  // BUG #9:是否 headless 执行(从 profile execution_mode 推导)。
  // headless→MCP clarify+前端卡片;交互式→终端直接问人(卡片不显示)。
  headless?: boolean
  // context_json 原文(JSON 字符串)。前端 parse 后读 _active_execution 判断
  // story 是否曾启动过(active 但无 _active_execution = single-pass 创建后从未跑)。
  contextJson?: string | null
}

// Bug 列表项(BugsPage 列表使用)。
export interface BugSummary {
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

export interface AgentAction {
  action: 'launch' | 'skip'
  adapter?: string
  stage?: string
  focus?: string
  done_file?: string
  reason?: string
}

export interface ActionButton {
  label: string
  method: string
  path?: string
  confirm?: string
  variant?: 'primary' | 'danger'
}

export interface PlanStage {
  name: string
  focus?: string
  adapter?: string
  done?: boolean
}

export interface StageGate {
  completed_stage?: string
  next_stage?: string
  awaiting_confirm?: boolean
}

export interface Plan {
  plan_summary?: string
  actions?: AgentAction[]
  confirmed?: boolean
  stages?: PlanStage[]
  stage_gate?: StageGate | null
  // STORY-STATE-MODEL: Story 业务状态机视图(主进度条用)+ 状态闸
  lifecycle_state?: string
  story_states?: StoryStateView[]
  story_state_gate?: StoryStateGate | null
}

// STORY-STATE-MODEL: Story 业务状态(开发/测试/上线)的一个节点视图
export interface StoryStateView {
  name: string
  stages: string[]
  current: boolean
  done: boolean
  done_count: number
  total: number
}

export interface StoryStateGate {
  from?: string
  to?: string
  awaiting_confirm?: boolean
  label?: string
}

// design 逐问澄清 HITL(runbook 块4):claude 遇关键岔路暂停等人答。
export interface ClarifyQuestion {
  id?: string | null
  header?: string
  question: string
  options: string[]
  context?: string | null
}
export interface ClarifyState {
  ok: boolean
  waiting: boolean
  status?: string
  question?: ClarifyQuestion
}

export interface LoopRound {
  stage: string
  loop_decision?: string
  loop_rounds?: number
  trajectory_score?: number | null
  summary?: string
  quality?: string | number
  issues_count?: number
  loopType?: 'plan' | 'code'
}

export interface LoopTrace {
  plan_loop?: { rounds?: LoopRound[] }
  code_loop?: { rounds?: LoopRound[] }
}

export interface Finding {
  id?: string | number
  severity?: string
  category?: string
  description?: string
  status?: string
  source?: string
  location?: string
}

export interface FindingsResponse {
  findings: Finding[]
}

export interface GateDecision {
  decision: string
  stage?: string
  reason_code?: string
  human_message?: string
  evidence?: Record<string, unknown>
}

export interface GateHistoryResponse {
  decisions: GateDecision[]
}

export interface TimelineStage {
  stage: string
  events?: Array<{ event_type: string; summary?: string }>
}

export interface Timeline {
  stages?: TimelineStage[]
}

export interface DebugEvent {
  event_type: string
  stage?: string
  detail?: string
  created_at?: string
}

export interface DebugPacket {
  stuckReasons?: string[]
  recentEvents?: DebugEvent[]
  state?: Record<string, unknown>
  [key: string]: unknown
}

export interface TokenUsage {
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  calls: number
  cost_cny: number
  by_stage: Record<string, number>
  by_model: Record<string, number>
}

export interface Stats {
  code_changes: number
  loop_rounds: number
  findings_open: number
  tokens: TokenUsage
}

export interface DiffFile {
  path: string
  additions: number
  deletions: number
  changes: number
}

export interface DiffResponse {
  source: 'gitlab' | 'local'
  current_branch: string
  base_branch: string
  diff_range: string
  mr_iid: number | null
  mr_url: string
  gitlab_url: string
  project_path?: string
  /** diff 所针对的 project_id(None = story workspace / 旧路径,未按单一项目过滤)。 */
  project_id?: number | null
  /** 实际 diff 的仓库路径(worktree_path 优先,否则 repo_path)。 */
  repo_path?: string
  /** 若用了 worktree 则为其路径;null = fallback 到主仓 repo_path。 */
  worktree_path?: string | null
  files: DiffFile[]
  total_additions: number
  total_deletions: number
  total_changes: number
  diff: string
  is_empty: boolean
}

export interface IntakePreview {
  storyKey: string
  sourceType: string
  sourceId: string
  title: string
  sourceUrl?: string
  action: 'generated' | 'manual_download_required' | 'needs_clarification' | 'failed'
  markdown: string
  summary?: string
  dingtalkLinks?: string[]
  questions?: string[]
  branch?: string
}

export interface Project {
  id: string | number
  name: string
  availability?: string
  repo_path?: string
  default_branch?: string
}

export interface WorkspaceOption {
  path: string
  name: string
  projectCount: number
  projects: string[]
}

export interface ProfileOption {
  name: string
  description: string
  stages: string[]
  execution_mode: string
}

/** story_project 行 — 一个 story 与一个 project 的绑定(分支 + worktree)。 */
export interface StoryProject {
  id: number
  story_key: string
  project_id: number
  branch?: string | null
  base_branch?: string | null
  worktree_path?: string | null
  /** unprepared | available | missing | stale | conflict | unknown */
  worktree_state: string
  workspace_type?: string | null
}

/** GET /api/story/{key}/context 的精简视图(CodeChangesTab 只用 projects + story_projects)。 */
export interface StoryContext {
  projects: Project[]
  story_projects: StoryProject[]
}

export interface Pattern {
  id: string | number
  pattern: string
  rule?: string
  confidence?: string | number
  applies_to?: string[]
  verification_count?: number
}

// ---- Fetch helpers ---------------------------------------------------------

export async function fetchJSON<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`${BASE}${path}`, init)
  if (!r.ok) throw new Error(`API ${r.status}: ${path}`)
  return r.json()
}

export async function apiAction(method: string, path: string): Promise<boolean> {
  try {
    const r = await fetch(`${BASE}${path}`, { method })
    return r.ok
  } catch {
    return false
  }
}

// Story APIs
export const storyApi = {
  list: () => fetchJSON<Story[]>('/api/story'),
  get: (key: string) => fetchJSON<Story>(`/api/story/${key}`),
  create: (data: { key: string; title?: string; content?: string; profile?: string; workspace?: string; autostart?: boolean }) =>
    fetchJSON<Story>('/api/story', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }),
  workspaces: () => fetchJSON<{ workspaces: WorkspaceOption[] }>('/api/workspaces'),
  profiles: () => fetchJSON<{ profiles: ProfileOption[] }>('/api/profiles'),
  projects: () => fetchJSON<{ projects: Project[] }>('/api/projects'),
  context: (key: string) => fetchJSON<StoryContext>(`/api/story/${key}/context`),
  previewIntake: (data: { source_type?: string; source_id: string; files?: File[] }) => {
    const form = new FormData()
    form.append('source_type', data.source_type || 'tapd')
    form.append('source_id', data.source_id)
    data.files?.forEach((file) => form.append('files', file))
    return fetchJSON<IntakePreview>('/api/intake/preview', {
      method: 'POST',
      body: form,
    })
  },
  advance: (key: string) => apiAction('PUT', `/api/story/${key}/advance`),
  // STORY-STATE-MODEL: Story 业务状态推进(开发→测试→上线),区别于 /advance(driver resume)
  advanceLifecycle: (key: string) => apiAction('POST', `/api/story/${key}/lifecycle/advance`),
  // 班车看板:改 story 归属班车(横向拖),train=null 清空回待分配
  setReleaseTrain: (key: string, train: string | null) =>
    fetchJSON<{ ok: boolean; releaseTrain: string | null }>(`/api/story/${key}/release-train`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ train }),
    }),
  skip: (key: string, stage: string) => apiAction('PUT', `/api/story/${key}/skip/${stage}`),
  abort: (key: string) => apiAction('POST', `/api/story/${key}/abort`),
  delete: (key: string) => apiAction('DELETE', `/api/story/${key}`),
  timeline: (key: string) => fetchJSON<Timeline>(`/api/story/${key}/timeline`),
  gateHistory: (key: string) => fetchJSON<GateHistoryResponse>(`/api/story/${key}/gate-history`),
  loopTrace: (key: string) => fetchJSON<LoopTrace>(`/api/story/${key}/loop-trace`),
  findings: (key: string, status = '', minSeverity = '') =>
    fetchJSON<FindingsResponse>(`/api/story/${key}/findings${status || minSeverity ? '?' : ''}${status ? `status=${status}` : ''}${status && minSeverity ? '&' : ''}${minSeverity ? `min_severity=${minSeverity}` : ''}`),
  dependencyGraph: (key: string) => fetchJSON<Record<string, unknown>>(`/api/story/${key}/dependency-graph`),
  debug: (key: string, limit = 50) => fetchJSON<DebugPacket>(`/api/story/${key}/debug?limit=${limit}`),
}

// Pattern APIs
export const patternApi = {
  list: (status = 'active') => fetchJSON<{ patterns: Pattern[] }>(`/api/patterns?status=${status}`),
  approve: (id: string | number) => apiAction('POST', `/api/patterns/${id}/approve`),
  reject: (id: string | number) => apiAction('POST', `/api/patterns/${id}/reject`),
}

// Terminal APIs
export const terminalApi = {
  spawn: (storyKey: string) => apiAction('POST', `/api/pty/${storyKey}/spawn`),
  kill: (storyKey: string) => apiAction('DELETE', `/api/pty/${storyKey}`),
  info: (storyKey: string) => fetchJSON<Record<string, unknown>>(`/api/session/terminal/${storyKey}`),
}

// Plan APIs (Agent mode)
export const planApi = {
  get: (key: string) => fetchJSON<Plan>(`/api/story/${key}/plan`),
  streamUrl: (key: string) => `/api/story/${key}/plan/stream`,
  confirm: (key: string) => apiAction('POST', `/api/story/${key}/plan/confirm`),
  regenerate: (key: string) => fetchJSON<Plan>(`/api/story/${key}/plan/regenerate`, { method: 'POST' }),
  updateAdapter: (key: string, stage: string, adapter: string) =>
    fetchJSON<{ ok: boolean; stage: string; adapter: string }>(
      `/api/story/${key}/plan/actions/${encodeURIComponent(stage)}`,
      {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ adapter }),
      },
    ),
  answer: (key: string, answer: string) =>
    fetchJSON<Record<string, unknown>>(`/api/story/${key}/answer`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ answer }),
    }),
  waitQuestion: (key: string) => fetchJSON<Record<string, unknown>>(`/api/story/${key}/wait`),
}

// Clarify APIs (design 逐问 HITL, runbook 块4/块8)
export const clarifyApi = {
  get: (key: string) => fetchJSON<ClarifyState>(`/api/story/${key}/clarify`),
  streamUrl: (key: string) => `/api/story/${key}/clarify/stream`,
  answer: (key: string, answer: string, id?: string | null) =>
    fetchJSON<Record<string, unknown>>(`/api/story/${key}/clarify/answer`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ answer, id }),
    }),
}

// Stats API
export const statsApi = {
  get: (key: string) => fetchJSON<Stats>(`/api/story/${key}/stats`),
}

// Diff API
export const diffApi = {
  get: (key: string, projectId?: number) =>
    fetchJSON<DiffResponse>(
      `/api/story/${key}/diff${projectId != null ? `?project_id=${projectId}` : ''}`,
    ),
}

// Multi-session PTY API
export const sessionApi = {
  list: (storyKey: string) =>
    fetchJSON<{ sessions: Array<{ session_id: string; adapter: string; stage: string; model: string; status: string; started_at: string }> }>(
      `/api/story/${storyKey}/sessions`
    ),
  spawn: (storyKey: string, adapter: string, model: string) =>
    fetchJSON<{ session_id: string }>(`/api/story/${storyKey}/sessions/spawn`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ adapter, model }),
    }),
  kill: (storyKey: string, sessionId: string) =>
    fetchJSON<{ ok: boolean }>(`/api/story/${storyKey}/sessions/${sessionId}`, {
      method: 'DELETE',
    }),
  wsUrl: (storyKey: string, sessionId: string) => {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
    return `${proto}//${location.host}/ws/pty/${storyKey}/${sessionId}`
  },
}

// ---- LLM audit (prompt/response/reasoning 正文审计) ------------------------

export interface LlmCallRow {
  id: number
  trace_id: number
  prompt_text?: string | null
  response_text?: string | null
  reasoning_text?: string | null
  tool_calls_json?: string | null
  created_at?: string
  stage?: string
  operation?: string
  model?: string
  prompt_tokens?: number
  completion_tokens?: number
  total_tokens?: number
  duration_ms?: number
  success?: number
  error?: string | null
}

export const auditApi = {
  calls: (key: string) => fetchJSON<{ story_key: string; calls: LlmCallRow[] }>(`/api/story/${key}/llm-calls`),
}

// ---- Versioned docs (story_doc / story_doc_version) ----
// doc_type is an open string: 'prd' | 'spec' | 'plan' | 'research' | custom.

export interface DocListItem {
  story_key: string
  doc_type: string
  title: string
  current_version: number
  updated_by: string
  updated_at: string
  local_path?: string
}

export interface DocContent {
  story_key: string
  doc_type: string
  title: string
  current_version: number
  latest_content: string
  local_path: string
  updated_by: string
  updated_at: string
}

export interface DocVersionSummary {
  story_key: string
  doc_type: string
  version: number
  change_reason: string
  author: string
  created_at: string
}

export interface DocVersionContent {
  story_key: string
  doc_type: string
  version: number
  content: string
  change_reason: string
  author: string
  created_at: string
}

export interface DocSearchHit {
  story_key: string
  doc_type: string
  title: string
  snippet: string
  rank: number
}

export const docApi = {
  list: (key: string) =>
    fetchJSON<{ docs: DocListItem[] }>(`/api/story/${key}/docs`),
  getDoc: (key: string, type: string) =>
    fetchJSON<DocContent>(`/api/story/${key}/docs/${type}`),
  saveDoc: (
    key: string,
    type: string,
    content: string,
    changeReason: string,
    title = '',
    author = 'user',
  ) =>
    fetchJSON<DocContent>(`/api/story/${key}/docs/${type}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content, change_reason: changeReason, title, author }),
    }),
  listVersions: (key: string, type: string) =>
    fetchJSON<{ versions: DocVersionSummary[] }>(`/api/story/${key}/docs/${type}/versions`),
  getVersion: (key: string, type: string, version: number) =>
    fetchJSON<DocVersionContent>(`/api/story/${key}/docs/${type}/versions/${version}`),
  diff: (key: string, type: string, a: number, b: number) =>
    fetchJSON<{ diff: string; a: number; b: number }>(
      `/api/story/${key}/docs/${type}/diff?a=${a}&b=${b}`,
    ),
  rollback: (key: string, type: string, version: number, reason: string, author = 'user') =>
    fetchJSON<DocContent>(`/api/story/${key}/docs/${type}/rollback/${version}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason, author }),
    }),
  search: (q: string, type = '', story = '') =>
    fetchJSON<{ query: string; results: DocSearchHit[] }>(
      `/api/docs/search?q=${encodeURIComponent(q)}${type ? `&type=${type}` : ''}${story ? `&story=${story}` : ''}`,
    ),
}
