> ⚠️ **历史快照（归档于 2026-07）**：描述的架构可能已被后续演进取代。当前架构见 [../../ARCHITECTURE.md](../../ARCHITECTURE.md)。本文件保留作决策记录（ADR），正文未修改。

---

# 问题：Workspace Git 约束与 Monorepo 支持

## 现状

### 架构

Story Lifecycle 按 profile 定义的阶段顺序执行，每个阶段：

1. **渲染 prompt**（`prompts/<stage>.md` 模板 + `{变量}` 替换）
2. **启动 CLI agent**（Claude Code / Codex 等）在 Zellij session 中
3. **等待 done 文件**（`.story/done/{story_key}/{stage}.json`）
4. **审查推进**（adversarial loop 或人机 decision）

### 当前约束方式

**软约束（prompt 文本）** —— 在 prompt 里写"请先切分支"、"不要在非 git 仓库修改"，但 AI 可以不遵守。

**无系统级硬约束** —— 没有代码层面的校验：
- 不检查 workspace 是不是 git 仓库
- 不自动扫描子目录找 git 仓库
- 不强制 AI 只能在特定路径操作
- 不校验 done 文件的字段完整性

### Workspace 模型

当前是**单一 workspace 目录**模型：

```
story create FEAT-001 -w /path/to/project
```

所有阶段都在这个目录下运行。`.story/` 目录也建在这里。

## 暴露的问题

### 场景：D:\hc-all

`D:\hc-all` 是一个 monorepo 目录，**本身不是 git 仓库**，但包含 17 个独立的 git 子仓库：

```
D:\hc-all/
  hc-user/          ← git repo (happy-cash/hc-user)
  hc-order/         ← git repo (happy-cash/hc-order)
  hc-config/        ← git repo (happy-cash/hc-config)
  frontends/
    hc-admin/       ← git repo (front/hc-admin)
  ... (共 17 个)
```

### 问题 1：workspace 设为非 git 目录

用户用 `story create 1065518 -w D:\hc-all`，workspace 是一个**非 git 目录**。AI agent 进入后：
- `git` 命令在根目录失败（不是仓库）
- AI 不知道应该在哪个子目录操作
- AI 直接在 `D:\hc-all` 下修改文件（不在任何 git 管理下）

### 问题 2：design 阶段没有产出仓库列表

`design.json` 只要求三个字段：

```json
{
  "spec_path": "...",
  "complexity": "S|M|L",
  "summary": "..."
}
```

**缺少 `affected_repos`** —— design 阶段应该识别并列出所有需要修改的仓库，但当前 schema 没有这个字段。后续阶段（implement、review）无从知道应该在哪些仓库操作。

### 问题 3：各仓库分支混乱

实际扫描发现：
- 3 个仓库已经切了 `feature/1065518-remove-other-occupation`（可能是 AI 自发操作）
- 2 个仓库还在上一个 story 的分支上
- 其余仓库在各种开发分支上

没有统一的"story 开始前先切分支"机制。

### 问题 4：prompt 约束无效

implement 阶段的 prompt 第 1 步写了"确保所有服务仓库在 feature 分支上"，但：
- AI 不知道"所有服务仓库"是哪些（design 没产出）
- workspace 根目录不是 git 仓库，`git checkout -b` 失败
- 没有系统级校验，AI 跳过了这一步直接改代码

## 需要的改动

### 短期（prompt 层面，已完成）

- [x] design.json schema 加 `affected_repos`
- [x] implement prompt 要求先读 `design.json` 再操作
- [x] review prompt 要求 diff `main...feature/{story_key}`

### 中期（系统层面，待实现）

**1. story init — 首次运行自动 setup**

story 创建或进入第一个 stage 前：
- 扫描 workspace 找所有 git 仓库（`find . -name .git -maxdepth 4`）
- 在每个仓库创建 `feature/{story_key}` 分支
- 将仓库列表写入 `context_json.affected_repos`
- 如果 workspace 下没有任何 git 仓库 → 警告用户

**2. stage 级硬约束**

- `execute_stage_node` 启动 CLI 前：校验 `affected_repos` 存在
- CLI agent 工作目录限制为 `affected_repos` 中的路径
- done 文件校验：`files_changed` 中的路径必须在 `affected_repos` 内

**3. Workspace 模型升级**

支持两种模式：
- `-w /path/to/git-repo` — 单仓库模式（当前）
- `-w /path/to/monorepo` — 多仓库模式（自动扫描子目录）

### 长期（架构层面）

- Stage 间传递结构化上下文（不只是 JSON 文件）
- CLI agent 的 tool 权限按 stage 约束（design 阶段不允许 Write/Edit）
- Worktree 级别的隔离（每个 story 有独立的 git worktree）
