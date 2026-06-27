# Story Lifecycle 开源路线图与优先级

> 日期：2026-05-23
> 目标：梳理各改进方向的当前状态、差距和行动项，确定先做什么

---

## 总体优先级排序

| 优先级 | 方向 | 目标 | 预估工作量 |
|--------|------|------|-----------|
| **P0** | Quick Start 体验 | 新用户 5 分钟内看到完整流程 | 2-3 天 |
| **P1** | Adapter 扩展 | 支持主流 AI CLI 工具 | 3-5 天 |
| **P2** | 质量飞轮 MVP | review 发现反哺下一轮执行 | 2-3 天 |
| **P3** | 持续迭代基建 | CI、CHANGELOG、版本节奏 | 1-2 天 |

**为什么这个顺序？** P0 解决"来了能不能用"的问题，P1 解决"能用在谁身上"的问题，P2 是差异化竞争力但需要 P0/P1 的用户基础来验证，P3 是长期健康度保证。

---

## P0: Quick Start 体验

### 现状问题

- `story serve` 启动后，用户需要手动创建 story、配置 profile、理解 handshake 协议
- 没有任何"一键体验"手段，新用户无法在 5 分钟内看到完整流程
- 当前唯一的 adapter 是 ClaudeAdapter，意味着用户必须先安装 Claude CLI

### 行动项

#### 1. `story demo` 命令（最高优先级）

利用已有的 `FakeStageTool`（E2E 测试基础设施），提供零依赖演示：

```bash
story demo
# 输出：
# ✓ 创建演示 story: demo-hello
# ✓ design 阶段完成 (模拟)
# ✓ implement 阶段完成 (模拟)
# ✓ test 阶段完成 (模拟)
# ✓ Story 完成！总耗时 3s
#
# 查看: story list
# 日志: story log demo-hello
```

实现路径：
- 在 `cli/` 下新增 `demo` 命令
- 复用 `tests/e2e/fake_tool.py` 的 `FakeStageTool`
- 加载 `profiles/minimal.yaml`，用 `happy_path.yaml` 的 payload 驱动
- 不需要 LLM、不需要真实 AI CLI、不需要 tmux

#### 2. `--dry-run` 模式

在 `story create` 和 `story serve` 中增加 dry-run 标志：

- 打印每个阶段会执行的 prompt（不真正执行）
- 显示 profile 解析结果、stage 序列
- 帮助用户理解配置是否正确

```bash
story create --dry-run my-feature --profile minimal
# 输出：
# Profile: minimal (design → implement → test)
# Adapter: claude
# Workspace: /path/to/workspace
# [dry-run] 不执行任何操作
```

#### 3. 分层引导路径

```
story demo          → 零依赖，30秒体验完整流程
story create --dry-run  → 验证配置正确
story create (rule-based)  → 无需 LLM key，使用规则路由
story create (LLM)   → 配置 LLM key，智能路由
story create (full)   → 完整 Cross-AI Review 飞轮
```

每层都在上一层基础上增加一个依赖，用户不会被一次性要求配置所有东西。

### 成功标准

- `pip install story-lifecycle && story demo` 在 30 秒内跑完
- README 里的 Quick Start 不超过 5 行命令

---

## P1: Adapter 扩展

### 现状

`BaseAdapter` 定义了 4 个方法：`switch_provider`、`launch_cmd`、`inject_prompt`、`cleanup`。目前只有 `ClaudeAdapter`。

### 行动项

#### 1. ShellAdapter（通用命令行适配器）

覆盖所有通过 shell 执行的 AI 工具（Aider、Codex CLI、Cursor 等）：

```python
class ShellAdapter(BaseAdapter):
    """通用 shell 命令适配器，通过配置文件定义命令模板。"""

    def __init__(self, config: dict):
        self.launch_template = config["launch_cmd"]
        self.inject_method = config.get("inject_method", "stdin")

    def launch_cmd(self, model: str) -> str:
        return self.launch_template.format(model=model)

    def inject_prompt(self, prompt: str, story_key: str, stage: str) -> str | None:
        if self.inject_method == "stdin":
            return f"cat /tmp/{story_key}_{stage}_prompt.txt"
        return None
```

配置在 `~/.story-lifecycle/adapters.yaml`：

```yaml
aider:
  launch_cmd: "aider --model {model}"
  inject_method: stdin

codex:
  launch_cmd: "codex --model {model}"
  inject_method: stdin
```

#### 2. ApiTool（API 调用模式）

当前 `BaseAdapter` 假设 AI 工具是 CLI 进程（需要 tmux + ttyd）。部分工具提供 API（如 Anthropic API、OpenAI API），应该支持直接 HTTP 调用：

```python
class ApiTool:
    """API-based execution — no tmux, no CLI."""

    def execute(self, state: dict, args: dict) -> dict:
        # 直接调用 API，不需要 poll_completion
        response = httpx.post(self.endpoint, json={...})
        return {"status": "done", "output": response.json()}
```

这意味着 `execute_stage_node` 需要一个分支：CLI 走 tmux + poll，API 走同步调用。

#### 3. Adapter 注册与发现

扩展 `__init__.py` 的 `get_adapter`：

```python
def get_adapter(name: str) -> BaseAdapter:
    builtins = {"claude": ClaudeAdapter, "shell": ShellAdapter}
    cls = builtins.get(name.lower())
    if not cls:
        # 从 adapters.yaml 加载
        config = load_adapter_config(name)
        if config:
            return ShellAdapter(config)
    if not cls:
        raise ValueError(...)
    return cls()
```

#### 4. Adapter 测试基类

```python
class AdapterTestBase:
    """所有 adapter 共享的契约测试。"""
    adapter: BaseAdapter

    def test_launch_cmd_returns_string(self): ...
    def test_inject_prompt_returns_string_or_none(self): ...
    def test_cleanup_does_not_crash(self): ...
```

### 支持矩阵（优先级排序）

| Adapter | 类型 | 难度 | 优先级 |
|---------|------|------|--------|
| ShellAdapter | 配置驱动 | 低 | 最高 |
| Aider (via ShellAdapter) | 配置 | 低 | 高 |
| Codex CLI (via ShellAdapter) | 配置 | 低 | 高 |
| Anthropic API | ApiTool | 中 | 中 |
| OpenAI API | ApiTool | 中 | 中 |
| Cursor | ShellAdapter | 低 | 低 |

### 成功标准

- 用户可以通过配置文件接入任意 AI CLI 工具
- 至少有 2 个 adapter 有完整的契约测试

---

## P2: 质量飞轮 MVP

### 设计原则

**先做最小闭环，不做完整系统。** 完整的 finding lifecycle（open → accepted → fixed → verified → learned）是 v1.0 的事。现在只做：review 结果存 DB → 压缩为 quality packet → 注入到 planner prompt。

### 行动项

#### 1. Quality Packet 构建（最小 MVP）

读取已有 `review_summary` 和 `trajectory_score`，压缩为 prompt 片段：

```python
def build_quality_packet(story_key: str) -> str:
    """从 DB 读取历史 review 结果，生成质量摘要注入 prompt。"""
    events = db.get_story_events(story_key)
    reviews = [e for e in events if e["event_type"] == "review"]

    if not reviews:
        return ""

    findings = []
    for r in reviews:
        payload = r.get("payload", {})
        if payload.get("verdict") == "revise":
            findings.append(f"- {payload.get('summary', '未提供摘要')}")

    if not findings:
        return ""

    return f"## 历史审查发现\n" + "\n".join(findings)
```

#### 2. 注入 Planner Prompt

在 `plan_stage_node` 渲染 prompt 时，追加 quality packet：

```python
# nodes.py plan_stage_node 中
quality_packet = build_quality_packet(state["story_key"])
if quality_packet:
    prompt += f"\n\n{quality_packet}"
```

#### 3. Reviewer 输出结构化

当前 `review_stage` 返回 dict，确保它包含：
- `verdict`: pass / revise / fail
- `summary`: 一句话描述
- `findings`: [{severity, location, description}]

这些字段写入 `stage_log` 的事件记录，是 quality packet 的数据源。

### 不做的事（推到 v1.0）

- Finding lifecycle 状态机（open/accepted/fixed/verified/learned）
- 跨 story 的 quality 仓库
- 自动化 fix 建议
- Quality dashboard

### 成功标准

- 一个 story 的 review 发现会出现在下一个 stage 的 planner prompt 中
- 可通过 `story log <key>` 查看 review 记录

---

## P3: 持续迭代基建

### 现状

- 没有 CI，测试只能本地跑
- 没有 CHANGELOG，用户不知道版本间有什么变化
- 版本号还在 0.x，没有明确的发布节奏

### 行动项

#### 1. GitHub Actions CI

```yaml
# .github/workflows/ci.yml
name: CI
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install -e ".[dev]"
      - run: ruff check src/
      - run: pytest --tb=short -q
```

#### 2. CHANGELOG

用 `keep-a-changelog` 格式，每次发版手动维护：

```markdown
## [0.4.0] - 2026-05-xx
### Added
- `story demo` 命令，零依赖体验完整流程
- ShellAdapter，通过配置接入任意 AI CLI 工具
- Quality Packet MVP，review 发现注入 planner prompt

## [0.3.0] - 2026-05-23
### Added
- Headless E2E 测试框架（FakeStageTool + Scenario DSL）
- Sub-story 拆分与委托
- Smart Orchestrator（LLM 路由 + review 循环）
```

#### 3. 版本节奏

```
v0.4.0 — Quick Start (story demo + --dry-run)
v0.5.0 — Adapter 扩展 (ShellAdapter + 配置驱动)
v0.6.0 — 质量飞轮 MVP
v0.7.0 — Story Source Integration (TAPD/Jira)
v0.8.0 — ApiTool (API 模式执行)
v0.9.0 — Quality Dashboard
v1.0.0 — 稳定版，API 锁定
```

### 成功标准

- 每次 push 自动跑测试和 lint
- 每个版本有 CHANGELOG 记录
- 用户知道下一版本会有什么

---

## 立即开始的事项

按优先级，建议从 **P0 的 `story demo`** 开始：

1. **`story demo` 命令** — 复用 FakeStageTool，30 秒跑完一个完整生命周期
2. **README Quick Start 更新** — 基于 demo 命令重写入门文档
3. **`--dry-run` 标志** — 帮助用户在真实执行前验证配置

这三件事完成后，项目就有了"来了就能体验"的能力，后续 adapter 扩展和质量飞轮才有用户来验证。
