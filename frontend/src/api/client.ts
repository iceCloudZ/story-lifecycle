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

export interface Plan {
  plan_summary?: string
  actions?: AgentAction[]
  confirmed?: boolean
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

export interface Stats {
  code_changes: number
  loop_rounds: number
  findings_open: number
}

export interface Project {
  id: string | number
  name: string
  availability?: string
  repo_path?: string
  default_branch?: string
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
  advance: (key: string) => apiAction('PUT', `/api/story/${key}/advance`),
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
  answer: (key: string, answer: string) =>
    fetchJSON<Record<string, unknown>>(`/api/story/${key}/answer`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ answer }),
    }),
  waitQuestion: (key: string) => fetchJSON<Record<string, unknown>>(`/api/story/${key}/wait`),
}

// Stats API
export const statsApi = {
  get: (key: string) => fetchJSON<Stats>(`/api/story/${key}/stats`),
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
