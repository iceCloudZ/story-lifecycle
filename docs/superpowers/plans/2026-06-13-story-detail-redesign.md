# Story Detail Page 重设计 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 StoryDetailPage 从单列信息堆砌重构为侧栏模块化布局，支持多 CLI 终端会话管理

**Architecture:** 侧栏 + 内容区布局，StoryDetailPage 作为容器管理 activeTab 状态。每个模块是独立 Tab 组件（ OverviewTab / CodeChangesTab / AdversarialLoopTab / TestTab / QualityGateTab / TerminalTab ），通过 `useQuery` 独立加载数据。终端模块支持多个 WebSocket 连接，Tab 切换不同 CLI 会话。

**Tech Stack:** React 18 + TypeScript + React Query + React Router + xterm.js + Vite

**Prerequisite (Phase 0):** 后端多 PTY 会话模型 (`/ws/pty/{story_id}/{session_id}`) + 聚合统计 API (`GET /api/story/{key}/stats`)

---

## 文件结构

```
frontend/src/
├── pages/
│   ├── StoryDetailPage.tsx        # 重写：容器组件，管理 activeTab
│   └── StoryDetailPage.css        # 重写：全局暗色主题 + 布局
├── components/
│   ├── StorySidebar.tsx           # 新增：侧栏导航
│   ├── StageProgress.tsx          # 新增：step progress bar（复用）
│   ├── ActionCard.tsx             # 新增：从 StoryDetailPage 提取
│   ├── OverviewTab.tsx            # 新增：概览模块
│   ├── CodeChangesTab.tsx         # 新增：代码变更模块
│   ├── AdversarialLoopTab.tsx     # 新增：对抗循环模块
│   ├── TestTab.tsx                # 新增：测试模块
│   ├── QualityGateTab.tsx         # 新增：质量 & Gate 模块
│   ├── TerminalTab.tsx            # 重构：多 CLI 会话管理
│   ├── TerminalPanel.tsx          # 保留作为单个 terminal view
│   └── TerminalPanel.css          # 已有，可能需要调整
├── hooks/
│   └── usePTYSessions.ts          # 新增：多 PTY 会话管理
├── api/
│   └── client.ts                  # 修改：添加 stats 和多 session API
└── App.css                        # 修改：添加页面级暗色覆盖
```

### 组件关系

```
StoryDetailPage (activeTab 状态, URL sync)
├── StorySidebar (模块列表 + 微型指示器)
├── OverviewTab (useQuery: plan, stats)
│   ├── StageProgress
│   └── ActionCard[]
├── CodeChangesTab (useQuery: timeline + files)
├── AdversarialLoopTab (useQuery: loopTrace)
├── TestTab (useQuery: findings + 测试数据)
├── QualityGateTab (useQuery: findings + gateHistory)
└── TerminalTab (usePTYSessions → WebSocket[])
    └── TerminalPanel (per session)
```

### 借口约定

```typescript
// OverviewTab 期望的聚合统计
interface StoryStats {
  code_changes: number      // 文件变更总数
  loop_rounds: number       // 对抗循环总轮次
  findings_open: number     // 未关闭的 Findings
}

// usePTYSessions 的 session 模型
interface PTYSession {
  sessionId: string
  storyKey: string
  adapter: string           // claude | codex
  stage: string             // design | implement | test
  model: string             // sonnet | opus | haiku
  status: 'running' | 'waiting' | 'exited'
  startedAt: string
}

// usePTYSessions 返回值
interface UsePTYSessionsResult {
  sessions: PTYSession[]
  activeSessionId: string | null
  setActiveSession: (id: string) => void
  spawnSession: (adapter: string, model: string) => Promise<void>
  killSession: (id: string) => Promise<void>
}
```

---

## Phase 1: 侧栏框架 + 概览页 + 终端重构

### Task 1: 添加 API 接口

**Files:**
- Modify: `frontend/src/api/client.ts`

- [ ] **Step 1: 添加 stats API 和多 session API**

```typescript
// client.ts — 在文件末尾追加

// Stats API
export const statsApi = {
  get: (key: string) => fetchJSON<{
    code_changes: number
    loop_rounds: number
    findings_open: number
  }>(`/api/story/${key}/stats`),
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
```

- [ ] **Step 2: 提交**

```bash
git add frontend/src/api/client.ts
git commit -m "feat: add stats and multi-session PTY APIs to client"
```

### Task 2: 创建 usePTYSessions hook

**Files:**
- Create: `frontend/src/hooks/usePTYSessions.ts`

- [ ] **Step 1: 实现 hook**

```typescript
import { useState, useEffect, useCallback } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { sessionApi, terminalApi } from '../api/client'

interface PTYSession {
  sessionId: string
  adapter: string
  stage: string
  model: string
  status: 'running' | 'waiting' | 'exited'
  startedAt: string
}

interface Props {
  storyKey: string
  autoConnect?: boolean
}

export default function usePTYSessions({ storyKey, autoConnect = false }: Props) {
  const qc = useQueryClient()

  const { data: sessionList } = useQuery({
    queryKey: ['sessions', storyKey],
    queryFn: () => sessionApi.list(storyKey),
    enabled: !!storyKey,
    refetchInterval: 5000, // 定期刷新 session 列表
  })

  const sessions: PTYSession[] = (sessionList?.sessions ?? []).map((s) => ({
    sessionId: s.session_id,
    adapter: s.adapter,
    stage: s.stage,
    model: s.model,
    status: s.status as PTYSession['status'],
    startedAt: s.started_at,
  }))

  const [activeSessionId, setActiveSessionId] = useState<string | null>(null)

  // 自动选择第一个活跃的 session
  useEffect(() => {
    if (!activeSessionId && sessions.length > 0) {
      const running = sessions.find((s) => s.status !== 'exited')
      setActiveSessionId(running?.sessionId ?? sessions[0].sessionId)
    }
  }, [sessions, activeSessionId])

  const spawnSession = useCallback(
    async (adapter: string, model: string) => {
      const result = await sessionApi.spawn(storyKey, adapter, model)
      qc.invalidateQueries({ queryKey: ['sessions', storyKey] })
      setActiveSessionId(result.session_id)
    },
    [storyKey, qc]
  )

  const killSession = useCallback(
    async (sessionId: string) => {
      await sessionApi.kill(storyKey, sessionId)
      qc.invalidateQueries({ queryKey: ['sessions', storyKey] })
      if (activeSessionId === sessionId) {
        setActiveSessionId(null)
      }
    },
    [storyKey, qc, activeSessionId]
  )

  return {
    sessions,
    activeSessionId,
    setActiveSession: setActiveSessionId,
    spawnSession,
    killSession,
  }
}
```

- [ ] **Step 2: 提交**

```bash
git add frontend/src/hooks/usePTYSessions.ts
git commit -m "feat: add usePTYSessions hook for multi-CLI session management"
```

### Task 3: 创建 StageProgress 组件

**Files:**
- Create: `frontend/src/components/StageProgress.tsx`

- [ ] **Step 1: 实现 step progress bar**

```typescript
import './StageProgress.css'

interface Stage {
  name: string
  status: 'completed' | 'running' | 'pending' | 'failed' | 'skipped'
}

interface Props {
  stages: Stage[]
  currentStage?: string
}

export default function StageProgress({ stages, currentStage }: Props) {
  return (
    <div className="stage-progress">
      <div className="sp-track">
        {stages.map((s, i) => {
          const isActive = s.name === currentStage
          const state = isActive ? 'running' : s.status
          return (
            <div key={s.name} className={`sp-step sp-${state}`}>
              {i > 0 && <div className="sp-line" />}
              <div className="sp-dot" />
              <div className="sp-label">
                <span className="sp-name">{s.name}</span>
                <span className="sp-status-text">{state}</span>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
```

- [ ] **Step 2: 创建 StageProgress.css**

```css
.stage-progress {
  padding: 16px 0;
}
.sp-track {
  display: flex;
  align-items: flex-start;
  gap: 0;
}
.sp-step {
  display: flex;
  flex-direction: column;
  align-items: center;
  flex: 1;
  position: relative;
  min-width: 80px;
}
.sp-dot {
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: #21262d;
  border: 2px solid #30363d;
  z-index: 1;
  flex-shrink: 0;
}
.sp-line {
  position: absolute;
  top: 7px;
  left: -50%;
  right: 50%;
  height: 2px;
  background: #30363d;
  z-index: 0;
}
.sp-completed .sp-dot { background: #238636; border-color: #3fb950; }
.sp-completed .sp-line { background: #238636; }
.sp-running .sp-dot { background: #58a6ff; border-color: #79c0ff; animation: sp-pulse 2s infinite; }
.sp-failed .sp-dot { background: #f85149; border-color: #ff7b72; }
.sp-skipped .sp-dot { background: #484f58; border-color: #6e7681; }
@keyframes sp-pulse {
  0%, 100% { box-shadow: 0 0 0 0 rgba(88, 166, 255, 0.4); }
  50% { box-shadow: 0 0 0 6px rgba(88, 166, 255, 0); }
}
.sp-label {
  margin-top: 6px;
  text-align: center;
}
.sp-name {
  display: block;
  font-size: 12px;
  font-weight: 600;
  color: #e6edf3;
}
.sp-status-text {
  display: block;
  font-size: 10px;
  color: #8b949e;
  margin-top: 2px;
}
```

- [ ] **Step 3: 提交**

```bash
git add frontend/src/components/StageProgress.tsx frontend/src/components/StageProgress.css
git commit -m "feat: add StageProgress step progress bar component"
```

### Task 4: 提取 ActionCard 组件

**Files:**
- Create: `frontend/src/components/ActionCard.tsx`
- Modify: `frontend/src/pages/StoryDetailPage.tsx` (删除内置的 ActionCard 定义)

- [ ] **Step 1: 提取 ActionCard**

```typescript
// ActionCard.tsx
interface AgentAction {
  action: 'launch' | 'skip'
  adapter?: string
  stage?: string
  focus?: string
  done_file?: string
  reason?: string
}

const ADAPTER_ICON: Record<string, string> = {
  claude: '🟠',
  codex: '🟢',
}

export default function ActionCard({ action, index }: { action: AgentAction; index: number }) {
  if (action.action === 'skip') {
    return (
      <div className="action-card action-skip">
        <div className="ac-header">
          <span className="ac-index">#{index + 1}</span>
          <span className="ac-icon">⏭️</span>
          <span className="ac-stage">{action.stage}</span>
          <span className="ac-badge ac-skip-badge">SKIP</span>
        </div>
        <div className="ac-reason">{action.reason}</div>
      </div>
    )
  }

  return (
    <div className="action-card action-launch">
      <div className="ac-header">
        <span className="ac-index">#{index + 1}</span>
        <span className="ac-icon">{ADAPTER_ICON[action.adapter ?? 'claude'] ?? '🔧'}</span>
        <span className="ac-stage">{action.stage}</span>
        <span className="ac-badge ac-adapter-badge">{action.adapter}</span>
      </div>
      {action.focus && <div className="ac-focus">{action.focus}</div>}
    </div>
  )
}
```

- [ ] **Step 2: 从 StoryDetailPage.tsx 删除内联的 ActionCard 和 ADAPTER_ICON 定义，改为 import**

在 StoryDetailPage.tsx 顶部添加：
```typescript
import ActionCard from '../components/ActionCard'
```
删除文件中原有的 `function ActionCard({ action, index })` 函数体和 `ADAPTER_ICON` 常量。

- [ ] **Step 3: 提交**

```bash
git add frontend/src/components/ActionCard.tsx frontend/src/pages/StoryDetailPage.tsx
git commit -m "refactor: extract ActionCard to standalone component"
```

### Task 5: 创建 StorySidebar 组件

**Files:**
- Create: `frontend/src/components/StorySidebar.tsx`
- Create: `frontend/src/components/StorySidebar.css`

- [ ] **Step 1: 实现侧栏**

```typescript
import './StorySidebar.css'

interface Module {
  id: string
  icon: string
  label: string
  badge?: number          // 微型指示器数字
  badgeVariant?: 'default' | 'danger'
}

interface Props {
  storyKey: string
  storyTitle: string
  storyStatus: string
  modules: Module[]
  activeModule: string
  onModuleChange: (id: string) => void
}

export default function StorySidebar({ storyKey, storyTitle, storyStatus, modules, activeModule, onModuleChange }: Props) {
  return (
    <aside className="story-sidebar">
      <div className="ss-story-info">
        <div className="ss-title" title={storyKey}>{storyTitle || storyKey}</div>
        <div className="ss-status">{storyStatus}</div>
      </div>
      <nav className="ss-nav">
        {modules.map((m) => (
          <button
            key={m.id}
            className={`ss-nav-item ${activeModule === m.id ? 'active' : ''}`}
            onClick={() => onModuleChange(m.id)}
          >
            <span className="ss-icon">{m.icon}</span>
            <span className="ss-label">{m.label}</span>
            {m.badge != null && (
              <span className={`ss-badge ${m.badgeVariant === 'danger' ? 'ss-badge-danger' : ''}`}>
                {m.badge}
              </span>
            )}
          </button>
        ))}
      </nav>
    </aside>
  )
}
```

- [ ] **Step 2: 创建 StorySidebar.css**

```css
.story-sidebar {
  width: 180px;
  min-width: 180px;
  background: #161b22;
  border-right: 1px solid #30363d;
  display: flex;
  flex-direction: column;
  overflow-y: auto;
}
.ss-story-info {
  padding: 12px;
  border-bottom: 1px solid #21262d;
}
.ss-title {
  font-size: 13px;
  font-weight: 700;
  color: #e6edf3;
  line-height: 1.4;
  word-break: break-all;
}
.ss-status {
  font-size: 10px;
  color: #8b949e;
  margin-top: 4px;
}
.ss-nav {
  padding: 8px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.ss-nav-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: #8b949e;
  font-size: 12px;
  cursor: pointer;
  transition: all 0.15s;
  text-align: left;
  width: 100%;
}
.ss-nav-item:hover {
  background: #21262d;
  color: #c9d1d9;
}
.ss-nav-item.active {
  background: #1f6feb22;
  color: #58a6ff;
  border-left: 2px solid #58a6ff;
}
.ss-icon { font-size: 14px; flex-shrink: 0; }
.ss-label { flex: 1; }
.ss-badge {
  background: #30363d;
  color: #8b949e;
  font-size: 10px;
  padding: 1px 5px;
  border-radius: 8px;
  min-width: 16px;
  text-align: center;
}
.ss-badge-danger {
  background: #3d1f1f;
  color: #f85149;
}
```

- [ ] **Step 3: 提交**

```bash
git add frontend/src/components/StorySidebar.tsx frontend/src/components/StorySidebar.css
git commit -m "feat: add StorySidebar with module navigation and micro-indicators"
```

### Task 6: 创建 OverviewTab 组件

**Files:**
- Create: `frontend/src/components/OverviewTab.tsx`

- [ ] **Step 1: 实现概览页**

```typescript
import { useQuery } from '@tanstack/react-query'
import { storyApi, statsApi, planApi } from '../api/client'
import StageProgress from './StageProgress'
import ActionCard from './ActionCard'

interface Props {
  storyKey: string
  detail: any              // story detail from parent
  planData: any            // plan data from parent
  resolvedActions: any[]   // Agent actions
  isConfirmed: boolean
  onConfirmPlan: () => void
  onRegeneratePlan: () => void
  onAction: (action: any) => void
  actions: any[]           // status-based action buttons
  onTabChange: (tabId: string) => void
}

export default function OverviewTab({
  storyKey, detail, planData, resolvedActions, isConfirmed,
  onConfirmPlan, onRegeneratePlan, onAction, actions, onTabChange,
}: Props) {
  const { data: stats } = useQuery({
    queryKey: ['stats', storyKey],
    queryFn: () => statsApi.get(storyKey),
    enabled: !!detail,
  })

  const stages = [
    { name: 'design', status: 'pending' as const },
    { name: 'implement', status: 'pending' as const },
    { name: 'test', status: 'pending' as const },
  ]

  return (
    <div className="tab-content overview-tab">
      {/* 顶栏 */}
      <div className="ot-header">
        <span className="ot-key">{detail.storyKey}</span>
        <span className="ot-updated">更新: {detail.updatedAt}</span>
      </div>

      {/* 进度条 */}
      <StageProgress stages={stages} currentStage={detail.currentStage} />

      {/* 信息卡片 */}
      <div className="ot-info-grid">
        <div className="ot-info-card">
          <div className="ot-info-label">Profile</div>
          <div className="ot-info-value">{detail.profile}</div>
        </div>
        <div className="ot-info-card">
          <div className="ot-info-label">重试次数</div>
          <div className="ot-info-value">{detail.executionCount} / 3</div>
        </div>
        <div className="ot-info-card">
          <div className="ot-info-label">来源</div>
          <div className="ot-info-value">
            {detail.sourceType ? `${detail.sourceType} · ${detail.priority || '-'}` : '-'}
          </div>
        </div>
      </div>

      {/* Agent 规划区 */}
      {detail.status === 'planning' && resolvedActions.length > 0 && (
        <div className="ot-plan-section">
          <h3>🤖 Agent 规划</h3>
          <div className="action-cards">
            {resolvedActions.map((a, i) => (
              <ActionCard key={i} action={a} index={i} />
            ))}
          </div>
        </div>
      )}

      {/* 操作按钮 */}
      <div className="ot-actions">
        {detail.status === 'planning' && !isConfirmed && resolvedActions.length > 0 && (
          <>
            <button className="btn btn-primary" onClick={onConfirmPlan}>
              ✅ 确认并执行 ({resolvedActions.filter((a: any) => a.action === 'launch').length} 步)
            </button>
            <button className="btn" onClick={onRegeneratePlan}>
              🔄 重新规划
            </button>
          </>
        )}
        {actions.map((a: any) => (
          <button
            key={a.label}
            className={`btn ${a.variant === 'danger' ? 'btn-danger' : ''} ${a.variant === 'primary' ? 'btn-primary' : ''}`}
            onClick={() => onAction(a)}
          >
            {a.label}
          </button>
        ))}
      </div>

      {/* 快捷统计 */}
      {stats && (
        <div className="ot-stats">
          <button className="ot-stat-card" onClick={() => onTabChange('code')}>
            <div className="ot-stat-num">{stats.code_changes}</div>
            <div className="ot-stat-label">代码变更</div>
          </button>
          <button className="ot-stat-card" onClick={() => onTabChange('loop')}>
            <div className="ot-stat-num">{stats.loop_rounds}</div>
            <div className="ot-stat-label">循环轮次</div>
          </button>
          <button className="ot-stat-card" onClick={() => onTabChange('quality')}>
            <div className="ot-stat-num" style={{ color: stats.findings_open > 0 ? '#f85149' : '#3fb950' }}>
              {stats.findings_open}
            </div>
            <div className="ot-stat-label">Findings 待处理</div>
          </button>
        </div>
      )}
    </div>
  )
}
```

- [ ] **Step 2: 提交**

```bash
git add frontend/src/components/OverviewTab.tsx
git commit -m "feat: add OverviewTab with progress bar, plan area, and quick stats"
```

### Task 7: 重构 TerminalTab（多 CLI 会话）

**Files:**
- Create: `frontend/src/components/TerminalTab.tsx`

- [ ] **Step 1: 实现多会话终端**

```typescript
import usePTYSessions from '../hooks/usePTYSessions'
import TerminalPanel from './TerminalPanel'
import './TerminalTab.css'

interface Props {
  storyKey: string
  status: string         // story status — 只有 active 时自动连接
}

export default function TerminalTab({ storyKey, status }: Props) {
  const { sessions, activeSessionId, setActiveSession, spawnSession, killSession } =
    usePTYSessions({ storyKey, autoConnect: status === 'active' })

  return (
    <div className="tab-content terminal-tab">
      {/* Session tabs */}
      <div className="tt-session-tabs">
        {sessions.map((s) => (
          <button
            key={s.sessionId}
            className={`tt-session-tab ${s.sessionId === activeSessionId ? 'active' : ''}`}
            onClick={() => setActiveSession(s.sessionId)}
          >
            <span className="tt-adapter-icon">
              {s.adapter === 'claude' ? '🟠' : '🟢'}
            </span>
            <span className="tt-session-label">
              {s.stage} · {s.adapter}
            </span>
            <span className={`tt-status-dot tt-${s.status}`} />
          </button>
        ))}
        {/* 启动新 session 按钮 */}
        {sessions.length === 0 && (
          <div className="tt-empty">
            <p>暂无 CLI 会话</p>
          </div>
        )}
      </div>

      {/* Active terminal */}
      {activeSessionId ? (
        <div className="tt-terminal-area">
          <TerminalPanel storyKey={activeSessionId} autoConnect />
          <div className="tt-session-info">
            {sessions.filter(s => s.sessionId === activeSessionId).map(s => (
              <span key={s.sessionId}>会话: {s.sessionId} | 启动: {s.startedAt}</span>
            ))}
          </div>
        </div>
      ) : (
        <div className="tt-no-session">选择或启动一个终端会话</div>
      )}
    </div>
  )
}
```

- [ ] **Step 2: 创建 TerminalTab.css**

```css
.terminal-tab {
  display: flex;
  flex-direction: column;
  height: 100%;
}
.tt-session-tabs {
  display: flex;
  gap: 4px;
  padding-bottom: 8px;
  border-bottom: 1px solid #21262d;
  overflow-x: auto;
}
.tt-session-tab {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  border: 1px solid #30363d;
  border-radius: 6px 6px 0 0;
  background: #161b22;
  color: #8b949e;
  font-size: 11px;
  cursor: pointer;
  white-space: nowrap;
  transition: all 0.15s;
}
.tt-session-tab:hover { background: #21262d; color: #c9d1d9; }
.tt-session-tab.active {
  background: #1f6feb22;
  color: #58a6ff;
  border-bottom-color: transparent;
}
.tt-status-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
}
.tt-running { background: #3fb950; }
.tt-waiting { background: #f0883e; animation: sp-pulse 2s infinite; }
.tt-exited { background: #484f58; }
.tt-terminal-area {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.tt-session-info {
  padding: 4px 8px;
  font-size: 10px;
  color: #484f58;
  border-top: 1px solid #21262d;
}
.tt-empty, .tt-no-session {
  padding: 40px;
  text-align: center;
  color: #8b949e;
  font-size: 13px;
}
```

- [ ] **Step 3: 提交**

```bash
git add frontend/src/components/TerminalTab.tsx frontend/src/components/TerminalTab.css
git commit -m "feat: add TerminalTab with multi-CLI session tab switching"
```

### Task 8: 重写 StoryDetailPage（容器 + 布局）

**Files:**
- Rewrite: `frontend/src/pages/StoryDetailPage.tsx`
- Rewrite: `frontend/src/pages/StoryDetailPage.css`

- [ ] **Step 1: 重写 StoryDetailPage 为侧栏+内容布局**

```typescript
import { useState, useEffect } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useParams, useNavigate, useSearchParams } from 'react-router-dom'
import { storyApi, apiAction, planApi } from '../api/client'
import StorySidebar from '../components/StorySidebar'
import OverviewTab from '../components/OverviewTab'
import ActionCard from '../components/ActionCard'
import TerminalTab from '../components/TerminalTab'
import './StoryDetailPage.css'

const MODULES = [
  { id: 'overview', icon: '📊', label: '概览' },
  { id: 'code', icon: '💻', label: '代码变更' },
  { id: 'loop', icon: '🔁', label: '对抗循环' },
  { id: 'test', icon: '🧪', label: '测试' },
  { id: 'quality', icon: '🛡', label: '质量 & Gate' },
  { id: 'terminal', icon: '💻', label: '终端' },
]

const ACTIONS: Record<string, any[]> = {
  planning: [
    { label: '终止', method: 'POST', path: '/abort', confirm: '确定终止此 Story？', variant: 'danger' },
  ],
  active: [
    { label: '跳过阶段', method: 'PUT', path: '/skip/{stage}' },
    { label: '终止', method: 'POST', path: '/abort', confirm: '确定终止此 Story？', variant: 'danger' },
  ],
  paused: [
    { label: '继续执行', method: 'PUT', path: '/advance', variant: 'primary' },
    { label: '跳过阶段', method: 'PUT', path: '/skip/{stage}' },
    { label: '终止', method: 'POST', path: '/abort', confirm: '确定终止此 Story？', variant: 'danger' },
  ],
  blocked: [
    { label: '重试', method: 'PUT', path: '/advance', variant: 'primary' },
    { label: '终止', method: 'POST', path: '/abort', confirm: '确定终止此 Story？', variant: 'danger' },
  ],
  failed: [
    { label: '删除', method: 'DELETE', path: '', confirm: '确定删除？不可恢复。', variant: 'danger' },
  ],
  completed: [
    { label: '删除', method: 'DELETE', path: '', confirm: '确定删除？不可恢复。', variant: 'danger' },
  ],
  aborted: [
    { label: '删除', method: 'DELETE', path: '', confirm: '确定删除？不可恢复。', variant: 'danger' },
  ],
}

export default function StoryDetailPage() {
  const { key } = useParams<{ key: string }>()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const storyKey = key ?? ''
  const [searchParams, setSearchParams] = useSearchParams()

  // activeTab 从 URL query param 读取，默认 overview
  const activeTab = searchParams.get('tab') || 'overview'
  const setActiveTab = (tab: string) => setSearchParams({ tab })

  const { data: detail, refetch } = useQuery({
    queryKey: ['story', storyKey],
    queryFn: () => storyApi.get(storyKey),
    refetchInterval: 5000,
  })

  const [planTriggered, setPlanTriggered] = useState(false)
  const [streamingActions, setStreamingActions] = useState<any[]>([])
  const [planError, setPlanError] = useState('')

  const { data: planData } = useQuery({
    queryKey: ['plan', storyKey],
    queryFn: () => planApi.get(storyKey),
    enabled: !!detail && detail.status === 'planning',
    refetchInterval: planTriggered ? false : 5000,
  })

  // SSE stream
  useEffect(() => {
    if (detail?.status !== 'planning') return
    if (planData?.actions?.length) return
    if (planData?.plan_summary && !planData?.actions) return
    if (planTriggered) return
    setPlanTriggered(true)
    const es = new EventSource(planApi.streamUrl(storyKey))
    es.onmessage = (e) => {
      try {
        const d = JSON.parse(e.data)
        if (d.type === 'action') {
          setStreamingActions(prev => [...prev, d.action])
        } else if (d.type === 'done') {
          es.close()
          qc.invalidateQueries({ queryKey: ['plan', storyKey] })
        } else if (d.type === 'error') {
          setPlanError(d.message)
          es.close()
        }
      } catch { /* ignore */ }
    }
    es.onerror = () => { es.close(); setPlanTriggered(false); qc.invalidateQueries({ queryKey: ['plan', storyKey] }) }
    return () => es.close()
  }, [detail?.status, planData, planTriggered, storyKey, qc])

  const resolvedActions = streamingActions.length > 0 ? streamingActions : (planData?.actions ?? [])
  const isConfirmed = planData?.confirmed ?? false

  if (!storyKey) return <div className="loading">无效的 Story Key</div>
  if (!detail) return <div className="loading">加载中...</div>

  const actions = ACTIONS[detail.status] || []

  async function handleConfirmPlan() {
    const r = await fetch(`/api/story/${storyKey}/plan/confirm`, { method: 'POST' })
    if (r.ok) refetch()
    else alert(`确认失败: ${(await r.json()).detail || '未知错误'}`)
  }

  async function handleRegeneratePlan() {
    setPlanTriggered(false); setStreamingActions([]); setPlanError('')
    try { await planApi.regenerate(storyKey) } catch {}
    qc.invalidateQueries({ queryKey: ['plan', storyKey] })
  }

  async function handleAction(action: any) {
    if (action.confirm && !window.confirm(action.confirm)) return
    let url = `/api/story/${storyKey}`
    if (action.path === '/skip/{stage}') url += `/skip/${detail?.currentStage}`
    else if (action.path) url += action.path
    if (await apiAction(action.method, url)) {
      if (action.method === 'DELETE') navigate('/')
      else { refetch(); qc.invalidateQueries({ queryKey: ['timeline', storyKey] }) }
    }
  }

  // Compute module badges
  const moduleBadges: Record<string, { badge?: number; badgeVariant?: 'default' | 'danger' }> = {}
  // (badges will be populated when stats data is available in OverviewTab)

  const modules = MODULES.map(m => ({
    ...m,
    ...moduleBadges[m.id],
  }))

  return (
    <div className="story-detail-page-v2">
      <div className="sdpv2-topbar">
        <button className="btn btn-back" onClick={() => navigate('/')}>← 返回</button>
        {detail.lastError && <span className="sdpv2-error-badge">⚠ {detail.lastError}</span>}
      </div>
      <div className="sdpv2-body">
        <StorySidebar
          storyKey={storyKey}
          storyTitle={detail.title || storyKey}
          storyStatus={detail.status}
          modules={modules}
          activeModule={activeTab}
          onModuleChange={setActiveTab}
        />
        <div className="sdpv2-content">
          {activeTab === 'overview' && (
            <OverviewTab
              storyKey={storyKey}
              detail={detail}
              planData={planData}
              resolvedActions={resolvedActions}
              isConfirmed={isConfirmed}
              onConfirmPlan={handleConfirmPlan}
              onRegeneratePlan={handleRegeneratePlan}
              onAction={handleAction}
              actions={actions}
              onTabChange={setActiveTab}
            />
          )}
          {activeTab === 'code' && <div className="tab-placeholder">💻 代码变更 — Phase 2 实现</div>}
          {activeTab === 'loop' && <div className="tab-placeholder">🔁 对抗循环 — Phase 3 实现</div>}
          {activeTab === 'test' && <div className="tab-placeholder">🧪 测试 — Phase 2 实现</div>}
          {activeTab === 'quality' && <div className="tab-placeholder">🛡 质量 & Gate — Phase 2 实现</div>}
          {activeTab === 'terminal' && (
            <TerminalTab storyKey={storyKey} status={detail.status} />
          )}
        </div>
      </div>
    </div>
  )
}
```

- [ ] **Step 2: 重写 StoryDetailPage.css（保留已有样式，新增布局样式）**

```css
/* ---- V2 Layout ---- */
.story-detail-page-v2 {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: #0d1117;
  color: #e6edf3;
}
.sdpv2-topbar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 16px;
  background: #161b22;
  border-bottom: 1px solid #30363d;
  flex-shrink: 0;
}
.sdpv2-error-badge {
  color: #f85149;
  font-size: 12px;
  background: #3d1f1f;
  padding: 2px 10px;
  border-radius: 4px;
  max-width: 400px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sdpv2-body {
  display: flex;
  flex: 1;
  min-height: 0;
}
.sdpv2-content {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
}
.tab-content {
  height: 100%;
}
.tab-placeholder {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: #484f58;
  font-size: 14px;
}

/* Overview Tab styles */
.ot-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}
.ot-key {
  font-size: 16px;
  font-weight: 700;
  color: #58a6ff;
}
.ot-updated {
  font-size: 11px;
  color: #8b949e;
}
.ot-info-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 8px;
  margin-bottom: 16px;
}
.ot-info-card {
  background: #161b22;
  border: 1px solid #30363d;
  padding: 10px 12px;
  border-radius: 6px;
}
.ot-info-label {
  font-size: 10px;
  color: #8b949e;
  margin-bottom: 4px;
}
.ot-info-value {
  font-size: 13px;
  color: #e6edf3;
  font-weight: 500;
}
.ot-plan-section {
  background: #161b22;
  border: 1px solid #30363d;
  border-radius: 8px;
  padding: 12px;
  margin-bottom: 12px;
}
.ot-plan-section h3 {
  margin: 0 0 10px;
  font-size: 14px;
  color: #e6edf3;
}
.ot-actions {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin-bottom: 16px;
}
.ot-stats {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 8px;
}
.ot-stat-card {
  background: #161b22;
  border: 1px solid #30363d;
  border-radius: 6px;
  padding: 12px;
  text-align: center;
  cursor: pointer;
  transition: all 0.15s;
  color: inherit;
  font: inherit;
}
.ot-stat-card:hover {
  border-color: #58a6ff;
  background: #1f6feb11;
}
.ot-stat-num {
  font-size: 22px;
  font-weight: 700;
  color: #3fb950;
}
.ot-stat-label {
  font-size: 11px;
  color: #8b949e;
  margin-top: 4px;
}

/* Keep all existing styles below (ActionCard, Timeline, Gate, Findings, etc.) */
/* ... existing styles from old StoryDetailPage.css ... */
```

- [ ] **Step 3: 构建验证**

```bash
cd frontend && npm run build
```
Expected: Build succeeds with no TS errors.

- [ ] **Step 4: 提交**

```bash
git add frontend/src/pages/StoryDetailPage.tsx frontend/src/pages/StoryDetailPage.css
git commit -m "feat: rewrite StoryDetailPage with sidebar + content layout (Phase 1)"
```

---

## Phase 2: 代码变更 + 测试 + 质量&Gate（后续 Plan）

Phase 2 实现三个数据展示模块，因它们独立于核心布局，可在 Phase 1 稳定后再做。

### 模块概要

| 模块 | 数据源 | 核心交互 |
|------|--------|---------|
| CodeChangesTab | `timeline` + git diff API | 阶段 filter tabs + 统计栏 + 可展开文件列表 |
| TestTab | `findings` (测试相关) | 统计栏 + 用例表格 (测试点/覆盖/状态) |
| QualityGateTab | `findings` + `gateHistory` | sub-tabs (Findings/Gate) + 严重度着色列表 |

### 数据来源说明

- **代码变更**：当前后端 `.story-done/{stage}.json` 的 `files_changed` 字段 + 计划新增的 git diff API
- **测试**：从 test 阶段的 stage output 提取（可由后端聚合或前端从 stage_log 解析）
- **质量 & Gate**：复用现有 `findings` + `gateHistory` API，前端做视图整合

---

## Phase 3: 对抗循环 + URL 持久化 + 响应式（后续 Plan）

### 模块概要

| 模块 | 数据源 | 核心交互 |
|------|--------|---------|
| AdversarialLoopTab | `loopTrace` API | 阶段 filter + Code↔Review 对比卡片 + 轨迹评分 |

### 优化项

- **虚拟滚动**：代码变更文件列表 > 100 项时启用 react-window
- **URL 持久化**：`?tab=code` 已在 Phase 1 实现
- **响应式**：窗口 < 768px 时侧栏变为顶部下拉（`<select>` fallback）

---

## Phase 0: 后端依赖（前置）

### 需要新增的 API

1. **`GET /api/story/{key}/stats`** — 聚合统计
   ```json
   { "code_changes": 8, "loop_rounds": 4, "findings_open": 2 }
   ```

2. **`GET /api/story/{key}/sessions`** — 列出所有 PTY 会话
   ```json
   { "sessions": [{ "session_id": "pty-1", "adapter": "claude", "stage": "implement", "model": "sonnet", "status": "running", "started_at": "..." }] }
   ```

3. **`POST /api/story/{key}/sessions/spawn`** — 创建新 PTY 会话
   ```json
   { "adapter": "codex", "model": "sonnet" } → { "session_id": "pty-2" }
   ```

4. **`DELETE /api/story/{key}/sessions/{session_id}`** — 终止会话

5. **`WS /ws/pty/{story_id}/{session_id}`** — 多会话 WebSocket（原 `/ws/pty/{story_id}` 升级）

### 后端改动范围

- `api.py`：新增 4 个 REST 端点 + 修改 WS 路径
- `terminal/pty.py`：支持一个 story 多个 PTY 实例
- `db/models.py`：新增 `pty_session` 表或扩展 `story` 表
