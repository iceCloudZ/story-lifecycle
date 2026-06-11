const BASE = ''

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
  list: () => fetchJSON<any[]>('/api/story'),
  get: (key: string) => fetchJSON<any>(`/api/story/${key}`),
  create: (data: { key: string; title?: string; content?: string; profile?: string; workspace?: string; autostart?: boolean }) =>
    fetchJSON<any>('/api/story', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }),
  advance: (key: string) => apiAction('PUT', `/api/story/${key}/advance`),
  skip: (key: string, stage: string) => apiAction('PUT', `/api/story/${key}/skip/${stage}`),
  abort: (key: string, _reason = 'User abort') => apiAction('POST', `/api/story/${key}/abort`),
  delete: (key: string) => apiAction('DELETE', `/api/story/${key}`),
  timeline: (key: string) => fetchJSON<any>(`/api/story/${key}/timeline`),
  gateHistory: (key: string) => fetchJSON<any>(`/api/story/${key}/gate-history`),
  loopTrace: (key: string) => fetchJSON<any>(`/api/story/${key}/loop-trace`),
  findings: (key: string, status = '', minSeverity = '') =>
    fetchJSON<any>(`/api/story/${key}/findings${status || minSeverity ? '?' : ''}${status ? `status=${status}` : ''}${status && minSeverity ? '&' : ''}${minSeverity ? `min_severity=${minSeverity}` : ''}`),
  dependencyGraph: (key: string) => fetchJSON<any>(`/api/story/${key}/dependency-graph`),
  debug: (key: string, limit = 50) => fetchJSON<any>(`/api/story/${key}/debug?limit=${limit}`),
}

// Pattern APIs
export const patternApi = {
  list: (status = 'active') => fetchJSON<any>(`/api/patterns?status=${status}`),
  approve: (id: string) => apiAction('POST', `/api/patterns/${id}/approve`),
  reject: (id: string) => apiAction('POST', `/api/patterns/${id}/reject`),
}

// Terminal APIs
export const terminalApi = {
  spawn: (storyKey: string) => apiAction('POST', `/api/pty/${storyKey}/spawn`),
  kill: (storyKey: string) => apiAction('DELETE', `/api/pty/${storyKey}`),
  info: (storyKey: string) => fetchJSON<any>(`/api/session/terminal/${storyKey}`),
}
