> ⚠️ **历史快照（归档于 2026-07）**：描述的架构可能已被后续演进取代。当前架构见 [../../ARCHITECTURE.md](../../ARCHITECTURE.md)。本文件保留作决策记录（ADR），正文未修改。

---

# 设计文档：Workspace Onboarding 与 Project Profile

## 版本记录

| 版本 | 日期 | 说明 |
|---|---|---|
| v0.1 | 2026-05-27 | 首版设计，定义首次接管目录、项目事实确认、Project Profile 与 Story Start Refresh |

## 背景

StoryOS 的目标不是每次让 code agent 从零猜项目，而是让某个目录逐步变成“被 StoryOS 认识的 workspace”。

真实项目通常有这些问题：

- workspace root 不一定是 git repo，例如 `D:\hc-all` 下有多个独立 git 子仓库。
- 测试命令、启动命令、发布规则散落在 README、脚本、CI、配置文件和团队习惯里。
- code agent 可以读项目并推断上下文，但它的推断不能直接成为事实。
- 每个 Story 开始前重复深度分析会浪费 token，也容易产生不一致结论。

因此需要一个 **Workspace Onboarding** 流程：

```text
首次在目录运行 story
  -> deterministic scan
  -> optional Project Intelligence Probe
  -> observed facts / hypotheses
  -> user confirmation
  -> confirmed Project Profile
  -> later stories consume confirmed facts
```

这吸收了外部 repo memory / context generation 工具的经验：先做可审计扫描和项目本地 profile，再把 profile 作为 agent 的项目事实来源。但 StoryOS 更进一步：这些事实不仅给 agent 读，还参与编排、测试选择、风险门禁和诊断。

## 命名

推荐术语：

| 术语 | 含义 |
|---|---|
| Workspace Onboarding | 某个目录首次被 StoryOS 接管时的项目事实建立流程 |
| Project Bootstrap | Onboarding 中的首次扫描和初始化动作 |
| Project Profile | 用户确认后的项目事实基线 |
| Observed Fact | 系统扫描得到的事实，带 evidence，但尚未被用户确认 |
| Hypothesis | agent 或规则推断的结论，带 confidence 和 evidence |
| Confirmed Fact | 用户确认或编辑后的事实，可影响编排决策 |
| Story Start Refresh | 每个 Story 开始前的轻量项目事实刷新 |

本文档使用 **Workspace Onboarding** 作为能力名，**Project Profile** 作为落盘产物。

## 目标

P0.8 目标：

1. 首次在 workspace 运行 `story` 时识别是否已存在 Project Profile。
2. 如果没有，执行 deterministic scan，生成 Observed Facts。
3. 识别 workspace 是 single repo、multi repo、plain directory 还是 unknown。
4. 扫描 git repo、技术栈、测试命令候选、CI、文档资产、发布线索。
5. 可选调用 Project Intelligence Probe，让 code agent 做只读探查。
6. 用户确认/编辑/忽略扫描结果，形成 Confirmed Facts。
7. 写入 `.story/project/profile.json`。
8. 每个 Story 开始前做 Story Start Refresh，检查 repo/test/profile 是否漂移。

非目标：

1. P0.8 不自动修改代码。
2. P0.8 不自动切分支。
3. P0.8 不把 agent hypotheses 直接当成 confirmed facts。
4. P0.8 不做完整发布系统建模。
5. P0.8 不自动上传 Project Profile。

## 外部模式吸收

外部工具普遍有三个模式：

1. **Deterministic repo scan**
   - 扫描目录、语言、README、测试命令、CI 文件。
   - 优点是稳定、便宜、可复现。

2. **Repo-local memory / context file**
   - 把项目事实写成本地文件，让后续 agent 读取。
   - 优点是跨会话复用。

3. **Agent-assisted context generation**
   - 调用 code agent 综合 README、脚本、配置，生成项目上下文。
   - 优点是能理解隐含约定；风险是幻觉和越权。

StoryOS 的原则：

```text
deterministic scan 生成 observed facts
agent probe 生成 hypotheses 或带证据 facts
用户确认后才成为 confirmed facts
confirmed facts 才能影响编排决策
```

## 用户流程

### 首次进入 workspace

用户在目录运行：

```text
story
```

或创建 Story：

```text
story create 1065518 -w D:\hc-all
```

系统检查：

```text
D:\hc-all\.story\project\profile.json
```

如果不存在，进入 onboarding：

```text
No Project Profile found for D:\hc-all.

StoryOS can inspect this workspace and build a local Project Profile.

Detected:
- workspace type: multi_repo
- git repos: 17
- likely backend repos: 12
- likely frontend repos: 3
- test command candidates: 9
- CI files: 2

Actions:
[a] accept and save
[e] edit before save
[i] ignore repos
[p] run agent probe
[s] skip for now
```

### 用户确认

用户确认后写入 Project Profile。

未确认的内容不能作为编排硬约束，只能作为 observed facts 或 hints。

### 每个 Story 开始前

Story Start Refresh 执行轻量检查：

- repo inventory 是否变化
- confirmed repo 是否还存在
- 测试命令证据文件是否还存在
- 当前 branch/dirty 状态
- Project Profile 是否过期

如果发现漂移：

```text
Project Profile drift detected:
- repo hc-order missing
- test command evidence package.json changed

[u] update profile
[c] continue once
[b] block story
```

## Project Profile Schema

建议路径：

```text
{workspace_root}/.story/project/profile.json
```

schema：

```json
{
  "schema_version": 1,
  "workspace_root": "D:\\hc-all",
  "workspace_id": "sha256-of-normalized-path",
  "created_at": "2026-05-27T15:30:00+08:00",
  "updated_at": "2026-05-27T15:30:00+08:00",
  "workspace_type": "multi_repo",
  "confidence": "high",
  "repos": [
    {
      "id": "hc-user",
      "name": "hc-user",
      "relative_path": "hc-user",
      "git_root": "D:\\hc-all\\hc-user",
      "remote": "happy-cash/hc-user",
      "default_branch": "main",
      "repo_type": "backend",
      "confirmed": true,
      "evidence": [
        {"path": "hc-user/.git", "kind": "git_dir"},
        {"path": "hc-user/pom.xml", "kind": "build_file"}
      ]
    }
  ],
  "test_sources": [
    {
      "id": "hc-user-unit",
      "repo_id": "hc-user",
      "name": "unit",
      "command": "mvn test",
      "scope": "repo",
      "cost": "medium",
      "reliability": "unknown",
      "confirmed": false,
      "evidence": [
        {"path": "hc-user/pom.xml", "kind": "maven"}
      ]
    }
  ],
  "release_profile": {
    "scale": "multi_service",
    "requires_manual_confirm": true,
    "signals": [
      {"type": "multi_repo_count", "value": 17}
    ],
    "confirmed": false
  },
  "doc_assets": [
    {"path": "README.md", "kind": "readme"},
    {"path": "docs/", "kind": "docs_dir"}
  ],
  "facts": [],
  "hypotheses": [],
  "user_overrides": []
}
```

## Fact 模型

### Observed Fact

```json
{
  "id": "fact-test-command-hc-user",
  "type": "test_command",
  "value": "mvn test",
  "scope": "repo:hc-user",
  "source": "deterministic_scan",
  "confidence": "high",
  "confirmed": false,
  "evidence": [
    {"path": "hc-user/pom.xml", "kind": "maven"}
  ],
  "observed_at": "2026-05-27T15:30:00+08:00"
}
```

### Hypothesis

```json
{
  "id": "hypothesis-claim-module",
  "type": "module_mapping",
  "value": "claim calculation likely lives in hc-claim/src/main/java/...",
  "scope": "repo:hc-claim",
  "source": "agent_probe",
  "confidence": 0.72,
  "confirmed": false,
  "evidence": [
    {"path": "hc-claim/src/main/java/.../ClaimService.java", "kind": "source_file"}
  ],
  "observed_at": "2026-05-27T15:30:00+08:00"
}
```

### Confirmed Fact

Confirmed Fact 可以来自：

- 用户确认 observed fact。
- 用户手动新增。
- 用户将 hypothesis 提升为 fact。

只有 Confirmed Fact 可以用于：

- 自动选择测试命令。
- 作为 Policy 的硬约束。
- 阻塞 Story 执行。
- 注入 prompt 中作为“项目事实”。

## Deterministic Scan

### Workspace Type

分类：

| type | 条件 |
|---|---|
| `single_repo` | workspace root 是 git repo |
| `multi_repo` | workspace root 不是 git repo，但 max_depth 内存在多个 git repo |
| `plain_directory` | 无 git repo，但存在项目文件 |
| `empty_or_unknown` | 无有效项目信号 |

扫描规则：

- 从 workspace root 开始，默认 max_depth=4。
- 忽略 `.git` 内部、`node_modules`、`target`、`build`、`.venv`、`dist`。
- 发现 nested repo 时记录 git root，不深入扫描其 `.git`。

### Repo Inventory

每个 repo 采集：

- relative path
- git root
- current branch
- dirty status
- remote URL
- default branch guess
- language/build files
- repo type guess：backend/frontend/mobile/infra/docs/unknown

default branch guess 顺序：

1. `origin/HEAD`
2. `main`
3. `master`
4. 当前分支

### Test Discovery

候选命令：

| 文件/信号 | 命令候选 |
|---|---|
| `pom.xml` | `mvn test` |
| `build.gradle` | `./gradlew test` 或 `gradle test` |
| `package.json` | `npm test` / `pnpm test` / scripts 中 test/lint |
| `pyproject.toml` + pytest | `pytest` |
| `tox.ini` | `tox` |
| `go.mod` | `go test ./...` |
| `.github/workflows/*.yml` | CI jobs |
| `.gitlab-ci.yml` | CI stages |
| `Jenkinsfile` | Jenkins pipeline |

P0 不运行测试命令，只发现候选并要求用户确认。

### Release / Scale Signals

P0 只做轻量判断：

- repo count
- backend/frontend/infra 数量
- 是否存在 Dockerfile、docker-compose、k8s、helm
- 是否存在 db migration 目录
- 是否存在 Nacos/config 相关目录或文件
- CI/CD 文件数量

输出 scale：

```text
single_service | multi_service | frontend_backend | monorepo | multi_repo | unknown
```

## Project Intelligence Probe

Probe 是 deterministic scan 的补充，不是替代。

### Probe 触发

P0.8 支持手动触发：

```text
story project probe --question "找出 hc-all 的测试命令和发布规则"
```

或 onboarding 中选择：

```text
[p] run agent probe
```

后续可由系统自动建议，但不能自动执行。

### Probe 任务书

必须包含：

- 只读声明。
- 明确问题。
- 可读取路径范围。
- 禁止写文件、改配置、切分支、安装依赖。
- 输出 JSON schema。
- facts/hypotheses/open_questions 三段。
- evidence 必填。

示例：

```text
你是 Project Intelligence Probe。

任务：找出该 workspace 的测试命令、启动命令和发布线索。

约束：
- 只读，不要修改任何文件。
- 不要安装依赖。
- 不要切换 git 分支。
- 不要运行耗时测试。
- 只允许读取 workspace 下的 README、配置、脚本、CI 文件。

输出 raw JSON：
{
  "facts": [],
  "hypotheses": [],
  "open_questions": []
}
```

### Probe 校验

StoryOS 在落盘前校验：

- JSON 可解析。
- 每个 fact 有 `type/value/evidence`。
- evidence path 在 workspace 内。
- path 存在。
- command 不包含 destructive pattern。
- hypothesis confidence 在 0-1。
- 不接受无 evidence 的 fact。

destructive pattern 示例：

```text
rm -rf
git reset --hard
git checkout
git clean
del /s
Remove-Item -Recurse
drop table
```

## 用户确认

确认界面需要支持：

| 操作 | 说明 |
|---|---|
| accept | 全部接受为 confirmed |
| edit | 编辑某条 fact 的值 |
| ignore | 忽略 repo/test/doc asset |
| downgrade | 将 fact 降为 hypothesis |
| mark unreliable | 将测试命令标记为不可靠 |
| add note | 补充团队规则 |

P0 可以先用 CLI 交互实现，TUI 后续再接。

## Story Start Refresh

每个 Story 第一次进入执行前做轻量刷新：

```text
load Project Profile
  -> check repo paths exist
  -> check confirmed test evidence exists
  -> check repo current branch / dirty
  -> detect newly added repos shallowly
  -> compare profile hash
  -> produce refresh_report
```

refresh 不做深度 agent probe，不做全量扫描。

输出：

```json
{
  "status": "ok|drift|missing_profile",
  "drift": [
    {
      "type": "repo_missing",
      "repo_id": "hc-order",
      "severity": "error"
    }
  ],
  "warnings": []
}
```

处理策略：

- `missing_profile`：提示 onboarding。
- `drift` 且 severity=error：默认阻塞，用户可选择 continue once。
- warning：展示但不阻塞。

## CLI 设计

新增命令：

```text
story project inspect
story project inspect --json
story project onboard
story project onboard --force
story project confirm
story project probe --question "..."
story project refresh
```

命令职责：

| 命令 | 职责 |
|---|---|
| `inspect` | deterministic scan，输出 observed facts，不写 confirmed profile |
| `onboard` | 执行 scan，进入确认流程，写 Project Profile |
| `confirm` | 对已有 observed facts 做确认/编辑 |
| `probe` | 受控调用 code agent 只读探查 |
| `refresh` | 对现有 Project Profile 做轻量漂移检查 |

未配置 LLM 时：

- `inspect`、`onboard` 的 deterministic scan 可运行。
- `probe` 不可运行，提示运行 `story setup`。

## 与 Workspace / Repo Scope 的关系

Workspace Onboarding 输出的 repo inventory 是后续 Workspace / Repo Scope Protocol 的基础。

```text
Project Profile repos = workspace 可见仓库全集
design affected_repos = 当前 story 允许操作子集
repo scope gate = implement/review 前的硬校验
```

因此：

- onboarding 不自动为所有 repo 切分支。
- story init 只对 `affected_repos` 做 branch/dirty 校验。
- multi repo story 后续可基于 repo inventory 拆子任务。

## 与现有设计的关系

| 设计 | 关系 |
|---|---|
| `idea-storyos-project-intelligence-control-plane.md` | 本文是 Project Intelligence Layer 的初始化落地设计 |
| `idea-project-intelligence-pipeline.md` | 本文覆盖 pipeline 的 repo/test/project bootstrap 部分 |
| `problem-workspace-git-constraint.md` | 本文解决 workspace root 与 git repo 边界不一致的事实发现部分 |
| `design-board-diagnostics-panel.md` | diagnostics 可读取 Project Profile，辅助解释 workspace/repo 问题 |
| `roadmap-v0.5-to-v1.0.md` | 本文落在 v0.8 Project Intelligence Input Layer |

## 错误处理

| 场景 | 行为 |
|---|---|
| workspace 不存在 | CLI 返回错误 |
| workspace 无项目特征 | 标记 `empty_or_unknown`，允许用户跳过 |
| 无 git repo | 标记 `plain_directory`，不阻塞 onboarding，但后续 code execution 需 gate |
| multi repo 数量过多 | 只展示摘要，写完整 JSON |
| probe 输出非法 JSON | 不落盘，保存 raw output 到 diagnostics |
| probe fact 无 evidence | 降级为 hypothesis 或 rejected |
| 用户跳过 onboarding | story 可继续，但 Project Profile 状态为 missing，后续风险提示 |

## 测试策略

单元测试：

1. single repo workspace 识别。
2. multi repo workspace 识别。
3. ignored directories 不被深入扫描。
4. default branch guess。
5. test command candidate discovery。
6. Project Profile schema 序列化。
7. Probe 输出 schema 校验。
8. destructive command rejection。
9. Story Start Refresh drift detection。

集成测试：

1. 临时目录含多个 git repo，`story project inspect --json` 输出 repos。
2. 有 `pom.xml/package.json/pyproject.toml` 时发现测试命令。
3. `story project onboard` 写 `.story/project/profile.json`。
4. 删除一个 repo 后 `story project refresh` 报 drift。
5. 未配置 LLM 时 `probe` 提示配置，`inspect` 仍可用。

## 落地顺序

1. `orchestrator/project_profile.py`：schema、读写、路径。
2. `orchestrator/project_scan.py`：deterministic scan。
3. `cli/project.py inspect --json`。
4. `story project onboard` 的最小确认流程。
5. `story project refresh`。
6. `orchestrator/project_probe.py`：只读 agent probe + schema 校验。
7. roadmap 中的 Workspace / Repo Scope gate 消费 Project Profile。
8. TUI onboarding/confirm 入口。

## 结论

Workspace Onboarding 是 StoryOS 熟悉项目的入口。

它把“某个目录”从普通文件夹升级为：

```text
StoryOS-managed workspace
```

后续每个 Story 都基于 confirmed Project Profile 执行，而不是让 agent 临时猜项目结构、测试命令和仓库边界。

