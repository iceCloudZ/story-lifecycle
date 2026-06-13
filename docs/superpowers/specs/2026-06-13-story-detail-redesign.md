# Story Detail Page 重设计

**日期**: 2026-06-13
**状态**: 已确认

## 背景

当前 `StoryDetailPage.tsx` 将所有信息无差别堆砌在单一长列表中，存在以下问题：

- **信息过载**：所有 section 无条件全部展示，空状态占位多
- **无视觉权重**：header、timeline、gate、findings 等权重无区分
- **风格不统一**：Plan section 浅色背景，其余暗色
- **终端位置尴尬**：始终在底部，非执行阶段不需要却可见
- **状态感知弱**：没有整体进度条，靠 badge 拼凑

## 新布局架构

### 整体结构：侧栏 + 内容区

```
┌──────────┬──────────────────────────────┐
│ Sidebar  │  Content                     │
│ (180px)  │  (flex: 1)                   │
│          │                              │
│ Story     │  根据侧栏选中模块切换内容     │
│ 名称+状态 │                              │
│ ──────── │                              │
│ 概览   ←  │                              │
│ 代码变更  │                              │
│ 对抗循环  │                              │
│ 测试      │                              │
│ 质量&Gate │                              │
│ 终端      │                              │
└──────────┴──────────────────────────────┘
```

### 侧栏模块（6 个）

| # | 模块 | 说明 |
|---|------|------|
| 📊 | 概览 | 默认页：Story 信息、进度条、Agent 规划、操作按钮、快捷统计 |
| 💻 | 代码变更 | 按阶段筛选的 git diff、文件变更列表、变更统计 |
| 🔁 | 对抗循环 | 每阶段 Plan↔Review / Code↔Review 对抗轨迹 |
| 🧪 | 测试 | 测试用例、覆盖范围、通过/失败状态 |
| 🛡 | 质量 & Gate | Review 结论 + Findings 列表 + Gate 决策链 |
| 💻 | 终端 | 多 CLI 会话管理（tab 切换），xterm.js 渲染 |

## 各模块详细设计

### 📊 概览页

默认打开页面，聚合 Story 最核心信息和操作。

**布局（从上到下）**：
1. **顶栏**：Story key + 小型状态 badge + 更新时间
2. **进度条**（step progress bar）：design → implement → test，当前阶段高亮，已完成阶段绿点
3. **信息卡片**（3 列 grid）：Profile / 重试次数 / 来源及优先级
4. **Agent 规划区**（仅 `planning` 状态显示）：action cards + 确认并执行 / 重新规划 / 终止按钮
5. **快捷统计**（3 列 mini 卡片）：代码变更数 / 循环轮次 / Findings 数，点击可跳转到对应模块

**状态展示规则**：
- status（状态机状态）作为小型文字 badge，不突出
- 进度条作为主视觉，回答"到哪了、接下来是什么"

### 💻 代码变更

**顶部**：阶段 filter tabs（全部 / design / implement / test）
**统计栏**：文件变更数 / +行数 / -行数 / commit 数，随 filter 联动
**文件列表**：可折叠展开，每项显示：
  - 文件名 + 所属阶段 tag + +/- 统计
  - 展开后显示 inline diff（绿色新增 / 红色删除）
**数据来源**：`.story-done/{stage}.json` 的 `files_changed` + git diff

### 🔁 对抗循环

**顶部**：阶段 filter tabs
**列表**：每轮对抗一张卡片
- 左侧：Code 产出摘要
- 中间：→ 箭头
- 右侧：Review 结论
- 顶部条：Round N + 决策（pass/revise/fail）+ 轨迹评分变化
- 展开可看完整 reviewer 反馈和 optimizer 响应

### 🧪 测试

**统计栏**：总用例数 / 通过 / 失败 / 跳过
**用例表格**：
| 测试点 | 覆盖范围（文件名） | 状态 |
|--------|-------------------|------|
| approveContact 正常审批流程 | approve.ts | ✓ pass |
| 审批人权限不足时拒绝 | approve.ts | ✓ pass |
| deleteContact 权限校验失败 | delete.ts | ✗ fail |

**交互**：点击行展开失败详情（错误信息、堆栈等）

### 🛡 质量 & Gate

**顶部 sub-tabs**：Findings | Gate 决策

**Findings 列表**：
- 按严重度着色排序（HIGH=红 / MEDIUM=橙 / LOW=绿）
- 每行：严重度标签 + 类别 + 描述 + 状态
- 展开可看详细证据和验证结果

**Gate 决策时间线**：
- 左边框着色（pass=绿 / retry=橙 / fail=红）
- 每行：决策标签 + 阶段名 + 理由摘要

### 💻 终端

**顶部 tabs**：所有 CLI 会话（活跃 + 历史）
- 每个 tab：adapter 图标（🟠claude / 🟢codex）+ 阶段名 + 运行状态灯（绿=运行中 / 橙=等待输入 / 灰=已结束）
- 点击切换

**主终端区**：xterm.js 渲染完整 PTY 输出（ANSI 颜色、光标）

**底部信息栏**：会话 ID / 启动时间 / 运行时长 / 编码

**多 CLI 典型场景**：
- design：1 个 CLI
- implement Round 1：2 个 CLI 并行（claude 写代码 + codex 审查）
- implement Round 2：2 个 CLI（claude 修问题 + codex 复查）
- test：1 个 CLI

## 技术实现要点

### 文件结构

```
frontend/src/
├── pages/
│   └── StoryDetailPage.tsx    # 重写：侧栏 + 内容区布局
├── components/
│   ├── StorySidebar.tsx        # 新增：侧栏导航
│   ├── OverviewTab.tsx         # 新增：概览模块
│   ├── CodeChangesTab.tsx      # 新增：代码变更模块
│   ├── AdversarialLoopTab.tsx  # 新增：对抗循环模块
│   ├── TestTab.tsx             # 新增：测试模块
│   ├── QualityGateTab.tsx      # 新增：质量 & Gate 模块
│   ├── TerminalTab.tsx         # 重构：多 CLI 会话管理
│   ├── StageProgress.tsx       # 新增：step progress bar
│   └── ActionCard.tsx          # 已有：Agent action card
└── hooks/
    └── usePTYSessions.ts       # 新增：多 PTY 会话管理 hook
```

### 数据流

- 侧栏通过 `storyApi.get(storyKey)` 获取 Story 详情，提取 status + stage 列表
- 每个 Tab 组件独立使用 `useQuery` 获取对应数据（timeline / gateHistory / findings / plan 等）
- 终端模块通过 WebSocket `/ws/pty/{story_id}` 连接多个 PTY 会话
- 概览页的快捷统计跨模块汇总数据，点击跳转到对应模块

### 路由

页面路由不变：`/story/:key`。模块切换通过组件内部 state（`activeTab`），不走 URL 路由。

### 侧栏交互增强

- 模块图标旁显示微型状态指示器（如代码变更数 3、测试失败数 1），用户不切换 Tab 即可感知全局
- 当前激活 Tab 高亮背景 + 左侧边框强调

### Agent 规划区交互边界（MVP）

- **仅支持三个操作**：确认并执行、重新规划（Agent 重新生成）、终止
- **不允许手动修改** action 细节（如调整阶段顺序、修改 focus），避免前后端状态不一致
- 未来迭代可增加"编辑"模式

### 性能优化

- **概览页快捷统计**：通过后端轻量聚合 API `GET /api/story/{key}/stats` 获取，避免触发 N 个 Tab 数据请求
- **代码变更 Diff**：大型需求的 Diff 可能超万行，文件列表默认折叠，仅在展开单文件时加载该文件的 Diff；未来引入虚拟滚动
- **对抗循环**：默认折叠，仅展示最新一轮结果；提供"展开完整轨迹"选项；可考虑时间轴可视化

### 路由持久化

- 模块切换使用 URL Query Parameter（`?tab=code`）持久化当前 Tab 状态
- 支持刷新页面后恢复、分享链接

### 响应式设计

- 桌面端：180px 固定侧栏
- 移动端（<768px）：侧栏变为顶部下拉菜单或底部 Tab 栏

## 后端依赖

- **Phase 0 强依赖**：多 CLI 会话管理需要后端支持
- 当前 PTY 是 1:1 映射（`/ws/pty/{story_id}`），需扩展为多会话模型
- WebSocket 路径设计：`/ws/pty/{story_id}/{session_id}`，前端 Tab 绑定 `session_id`
- 新增聚合统计 API：`GET /api/story/{key}/stats`，返回变更数、循环轮次、Findings 数等轻量数据

## 实施路径

| Phase | 内容 | 依赖 |
|-------|------|------|
| **Phase 0** | 后端：多 PTY 会话模型改造（`/ws/pty/{story_id}/{session_id}`）+ 聚合统计 API | — |
| **Phase 1** | 前端：侧栏框架 + 概览页 + 终端模块重构，打通核心 Agent 交互流程 | Phase 0 |
| **Phase 2** | 前端：代码变更、测试、质量 & Gate 模块，替换旧视图 | Phase 1 |
| **Phase 3** | 前端：对抗循环模块 + 虚拟滚动 + 响应式适配 + 性能优化 | Phase 2 |

## 兼容性

- 保留现有 API 接口不变
- 现有 `TerminalPanel` 组件逐步迁移到 `TerminalTab`
- 保留现有 CSS 变量和暗色主题，新增模块样式统一用暗色背景
- `StoryDetailPage.css` 拆分为模块级 CSS 文件
