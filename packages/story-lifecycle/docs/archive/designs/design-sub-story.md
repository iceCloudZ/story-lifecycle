> ⚠️ **历史快照（归档于 2026-07）**：描述的架构可能已被后续演进取代。当前架构见 [../../ARCHITECTURE.md](../../ARCHITECTURE.md)。本文件保留作决策记录（ADR），正文未修改。

---

# Sub-story 子故事功能设计文档

> 日期：2026-05-23
> 状态：已评审，修订中
> 作者：zhaozihao
> 评审评分：8.5/10

---

## 1. 背景与问题

当前 Story Lifecycle 中，每个 story 都是独立运行的。但实际开发中最常见的场景是：

- 主故事完成 implement 后，发现 bug 需要修复
- 前后端联调时需要小幅调整
- 需求评审后需要补充或调整细节
- 实现方案有问题，需要重做

这些**后续工作**与主故事共享同一个工作区、同一份上下文（PRD、设计文档、评审记录），但目前用户只能从零创建一个新故事，手动重新关联这些资源。

**目标**：支持从父故事派生子故事，自动继承上下文，提供快速创建入口，同时保持独立的生命周期。

## 2. 核心概念

```
父故事（任意状态）
  │
  ├─ 子故事 A（bug-fix，从 implement 开始）
  ├─ 子故事 B（refinement，从 design 开始）
  └─ 子故事 C（自定义类型，从 implement 开始）
```

- **子故事是一个独立的 story**，拥有自己的 stage 执行流程
- 通过 `parent_key` 关联父故事
- 继承父故事的 workspace、profile、context_json
- 用户选择**起始阶段**和**类型**

## 3. 类型系统

类型是一个**用户可读的标签**，不影响执行流程。流程由起始阶段决定。

### 3.1 预设类型

| 类型 | 标识 | 默认起始阶段 | 描述 |
|------|------|-------------|------|
| 缺陷修复 | `bug-fix` | implement | 修复已知 bug |
| 联调适配 | `integration` | implement | 前后端联调、接口适配 |
| 需求补充 | `refinement` | design | 需求细节调整、补充 |
| 返工重做 | `redo` | design | 推翻现有方案重做 |

### 3.2 自定义类型

用户可输入任意文本作为类型标签，例如 `hotfix`、`perf-tuning`。自定义类型需手动选择起始阶段。

### 3.3 类型与流程的关系

```
类型 ──影响──→ 显示标签、badge 颜色
类型 ──影响──→ 默认起始阶段（用户可覆盖）
类型 ──不影响──→ 执行流程、stage 配置、prompt 模板
```

### 3.4 类型配置化

预设类型映射不硬编码，放在 `config.yaml` 中，用户可扩展：

```yaml
sub_story_types:
  bug-fix:
    label: "缺陷修复"
    color: red
    default_start_stage: implement
    description_template: "修复以下问题："
  integration:
    label: "联调适配"
    color: yellow
    default_start_stage: implement
    description_template: "前后端联调修改："
  refinement:
    label: "需求补充"
    color: blue
    default_start_stage: design
    description_template: "需求补充/调整："
  redo:
    label: "返工重做"
    color: orange
    default_start_stage: design
    description_template: "重做，原因："
```

好处：
- 用户可添加自定义类型（如 `hotfix`、`perf-tuning`），配好 label 和 default_start_stage 即可
- `description_template` 预填充描述字段，减少用户输入
- 新增类型无需改代码

## 4. 上下文继承

子故事创建时，从父故事继承以下内容：

| 继承项 | 来源 | 说明 |
|--------|------|------|
| workspace | 父故事.workspace | 共享同一工作目录 |
| profile | 父故事.profile | 使用同一份 stage 配置 |
| context_json | 父故事.context_json | 深拷贝，包含 prd_path、spec_path、review 结果等 |

**关键细节**：
- context_json 是**深拷贝**，子故事修改不影响父故事
- 子故事的 `prd_path` 等文件路径指向同一份文件（workspace 相同），不需要额外处理
- 继承发生在创建时，之后父子故事互不影响

### 4.1 Context 大小控制

深拷贝有性能风险（父故事有多个子故事时反复拷贝，context 可能包含完整 PRD 文本）。

```python
MAX_CONTEXT_SIZE = 1 * 1024 * 1024  # 1MB

def inherit_context(parent_key: str, parent_ctx_json: str, description: str) -> str:
    parent_ctx = json.loads(parent_ctx_json or "{}")
    if len(parent_ctx_json) > MAX_CONTEXT_SIZE:
        # 只继承可控大小字段，跳过大字段；按 JSON 序列化后的大小判断，避免丢失数字/布尔/列表/对象
        child_ctx = {
            "parent_ref": parent_key,
            "sub_description": description,
            "_skipped_fields": [],
        }
        for k, v in parent_ctx.items():
            try:
                serialized = json.dumps(v, ensure_ascii=False)
            except TypeError:
                child_ctx["_skipped_fields"].append(k)
                continue
            if len(serialized) <= 10_000:
                child_ctx[k] = v
            else:
                child_ctx["_skipped_fields"].append(k)
        return json.dumps(child_ctx, ensure_ascii=False)
    else:
        child_ctx = json.loads(json.dumps(parent_ctx))  # deep copy
        child_ctx["parent_ref"] = parent_key
        child_ctx["sub_description"] = description
        return json.dumps(child_ctx, ensure_ascii=False)
```

## 5. 数据模型

### 5.1 现有字段（已实现）

```sql
-- story 表中已有的字段
parent_key TEXT          -- 父故事的 story_key，顶层故事为 NULL
subtask_index INTEGER    -- 同一父故事下的序号，默认 0
```

### 5.2 新增字段

```sql
ALTER TABLE story ADD COLUMN sub_type TEXT;
-- sub_type: 子故事类型标识，如 "bug-fix"、"integration"
-- 顶层故事为 NULL
```

落地时需要同步修改 DB CRUD 层：
- `VALID_COLUMNS` 增加 `sub_type`
- `create_story()` / `upsert_story()` 支持传入并持久化 `sub_type`
- `GET /api/story`、`GET /api/story/{story_key}` 和子故事列表响应返回 `subType`
- `build_sub_summary()` 从 DB 读取 `sub_type`，缺失时按空字符串或 `custom` 兜底

### 5.3 现有查询（已实现）

```python
# 已有的子故事查询函数
db.get_sub_stories(parent_key)  # 按 subtask_index 排序
db.get_pending_parents()        # 查询 waiting_subtasks 状态的父故事
db.list_active_stories()        # 包含 waiting_subtasks 状态
```

## 6. 工作区并发控制

### 6.1 问题

父子故事共享 workspace，如果同时执行，两个 AI CLI 会同时修改同一份代码，产生写写冲突。Git 能解决版本冲突，但无法解决语义冲突。

### 6.2 互斥规则

采用方案 A：**工作区互斥锁只保护 AI 执行阶段，不保护调度和状态恢复。**

同一时刻，同一个 workspace 最多只有一个 story 进入真正会读写代码的 AI 执行阶段，例如 `execute_stage_node` / `StageTool.execute()` 启动 Claude、Codex、Aider 并等待 `.story-done/...json` 的过程。

以下调度动作不需要 workspace 锁：
- 创建子故事
- 父故事从 `waiting_subtasks` 恢复为 `active`
- 子故事依赖满足后从 `blocked` 改为 `active`
- 写 DB 状态、刷新 TUI、检查子故事是否结束

没拿到 workspace 锁时，不允许直接失败；应把 story 标记为 `waiting_workspace` 或进入等价的排队状态，由 watchdog / scheduler 稍后重试。

P0 约束：内存级 `threading.Lock` 只支持 `story serve` 单进程运行。多进程部署（例如多个 Uvicorn/Gunicorn worker）下内存锁无法跨进程互斥，P0 明确不支持；P1 再考虑文件锁或 DB 乐观锁。

P0 恢复规则：`waiting_workspace` 是持久化状态，服务启动和 watchdog 轮询时必须把它重新纳入调度。启动时可将遗留的 `waiting_workspace` 重置为 `active`，由调度器重新排队拿锁，避免服务重启后永久卡住。

```python
import threading

_workspace_locks: dict[str, threading.Lock] = {}
_workspace_lock_owners: dict[str, str] = {}

def acquire_workspace(workspace: str, story_key: str) -> bool:
    """只在 AI 执行阶段调用。成功返回 True，已有其他 story 执行则返回 False。"""
    lock = _workspace_locks.setdefault(workspace, threading.Lock())
    acquired = lock.acquire(blocking=False)
    if acquired:
        _workspace_lock_owners[workspace] = story_key
        return True
    return False

def release_workspace(workspace: str):
    """AI 执行阶段结束后释放 workspace 锁。"""
    lock = _workspace_locks.get(workspace)
    if lock and lock.locked():
        _workspace_lock_owners.pop(workspace, None)
        lock.release()
```

### 6.3 应用场景

| 场景 | 处理 |
|------|------|
| 父故事 waiting_subtasks，子故事 active | 正常，父故事不执行；子故事进入 AI 执行阶段时才拿 workspace 锁 |
| 手动恢复父故事 | 先处理未完成子故事；恢复动作本身不拿 workspace 锁 |
| 子故事全部结束，父故事自动恢复 | 恢复动作不拿 workspace 锁；父故事后续真正执行 AI 阶段时再排队拿锁 |
| 两个子故事都想执行 AI 阶段 | 串行排队，没拿到锁的一方进入 `waiting_workspace` |

## 7. API 设计

### 7.1 创建子故事

```
POST /api/story/{parent_key}/sub
```

请求体：

```json
{
  "sub_type": "bug-fix",
  "start_stage": "implement",
  "description": "修复用户登录后页面空白的问题"
}
```

字段说明：
- `sub_type`（可选）：类型标识，默认 null。支持预设类型或自定义文本
- `start_stage`（可选）：起始阶段，默认由 sub_type 推导，无 type 时默认 profile 的第一个 stage
- `description`（必填）：一句话描述要做什么，注入到起始阶段的 prompt 中

处理逻辑：

```python
def create_sub_story(parent_key, sub_type=None, start_stage=None, description=""):
    parent = db.get_story(parent_key)
    if not parent:
        raise 404

    # 禁止嵌套：父故事本身不能是子故事
    if parent["parent_key"] is not None:
        raise BusinessError("子故事不能嵌套创建")

    # 推导起始阶段（从配置读取，非硬编码）
    type_config = load_sub_types().get(sub_type, {})
    if not start_stage:
        start_stage = type_config.get("default_start_stage", first_stage_of_profile)

    # 继承 context（带大小控制）
    child_ctx = inherit_context(parent_key, parent["context_json"], description)

    # 创建子故事。key 生成存在并发竞态，必须依赖 DB UNIQUE 约束并重试。
    for attempt in range(3):
        siblings = db.get_sub_stories(parent_key)
        index = len(siblings)
        story_key = f"{parent_key}-sub-{index + 1}"
        try:
            db.create_story(
                story_key=story_key,
                title=description,
                workspace=parent["workspace"],
                profile=parent["profile"],
                current_stage=start_stage,
                parent_key=parent_key,
                subtask_index=index,
                sub_type=sub_type,
            )
            db.update_story(story_key, context_json=child_ctx)
            break
        except sqlite3.IntegrityError:
            if attempt == 2:
                raise BusinessError("子故事创建冲突，请重试")
            continue

    db.log_stage(story_key, "", "create_sub", f"type={sub_type}, from={parent_key}")

    # 父故事状态变更
    if parent["status"] == "active":
        db.update_story(parent_key, status="waiting_subtasks")

    return story_key
```

### 7.2 中止子故事

```
POST /api/story/{story_key}/abort
```

```python
def abort_story(story_key: str, reason: str = "User abort"):
    s = db.get_story(story_key)
    if not s:
        raise 404

    try:
        # 停止执行
        stop_story_execution(story_key)
        db.update_story(story_key, status="aborted", last_error=reason)
        db.log_stage(story_key, "", "abort", reason)
    finally:
        # abort/failed 等强制停止路径必须释放执行阶段锁，避免 workspace 永久卡住
        release_workspace(s["workspace"])

    # 如果是子故事，检查父故事是否可以恢复
    if s["parent_key"]:
        check_parent_resume(s["parent_key"])
```

中止与完成的区别：

这里拆成两个概念，避免混用"完成"和"结束"：
- `terminal`：子故事已经不会继续执行，可以从父故事等待集合中移除
- `successful`：子故事产出了可被父故事接纳的成功结果

| status | terminal | successful | 含义 |
|------|-----------|------------|------|
| `active` | 否 | 否 | 正在执行 |
| `paused` | 否 | 否 | 暂停中，未来还可能继续 |
| `waiting_workspace` | 否 | 否 | 等待同 workspace 的其他故事释放 AI 执行锁 |
| `blocked` | 否 | 否 | 卡住了，需要人工处理、重试或中止 |
| `completed` | 是 | 是 | 正常完成，结果可信 |
| `aborted` | 是 | 否 | 用户/系统中止，不再执行，但没有成功结果 |
| `failed` | 是 | 否 | 确认失败，不会继续 |

P0 规则：**所有子故事都 terminal 后，父故事可以脱离 `waiting_subtasks`；但如果存在 unsuccessful 子故事，父故事必须带着失败/中止标记进入人工确认或 review，不允许按全成功路径直接继续。**

### 7.3 手动恢复父故事

```
PUT /api/story/{parent_key}/resume
```

恢复时强制处理未完成子故事：

```python
def resume_parent(parent_key: str, strategy: str = "pause_subs"):
    parent = db.get_story(parent_key)
    resumable_statuses = {"waiting_subtasks", "paused"}
    if parent["status"] not in resumable_statuses:
        raise BusinessError("父故事不在可恢复状态")

    subs = db.get_sub_stories(parent_key)
    unfinished_subs = [
        s for s in subs
        if s["status"] not in ("completed", "aborted", "failed")
    ]

    if unfinished_subs:
        if strategy == "pause_subs":
            # 强制暂停所有未完成子故事
            for sub in unfinished_subs:
                stop_story_execution(sub["story_key"])
                db.update_story(sub["story_key"], status="paused")
                db.log_stage(sub["story_key"], "", "pause", "父故事手动恢复，子故事被暂停")
        elif strategy == "abort_subs":
            # 中止所有未完成子故事
            for sub in unfinished_subs:
                abort_story(sub["story_key"], "父故事恢复，子故事被中止")

    db.update_story(parent_key, status="active")
    db.log_stage(parent_key, "", "resume", "手动恢复")
    start_story_async(parent_key)
```

TUI 提示：

```
┌─ 恢复父故事 ─────────────────────────────────┐
│                                               │
│ 存在 2 个未完成的子故事：                       │
│   FEATURE-001-sub-1 [active]                  │
│   FEATURE-001-sub-2 [paused]                  │
│                                               │
│ [1] 暂停所有子故事后恢复（推荐）                │
│ [2] 中止所有子故事后恢复                        │
│ [3] 取消                                      │
└───────────────────────────────────────────────┘
```

### 7.4 查询子故事列表

```
GET /api/story/{parent_key}/subs
```

返回父故事下所有子故事，按 `subtask_index` 排序。

### 7.5 查询故事关系

现有 `GET /api/story/{story_key}` 响应扩展：

```json
{
  "storyKey": "FEATURE-001-sub-1",
  "parentKey": "FEATURE-001",
  "subType": "bug-fix",
  "subs": [],
  ...
}
```

```json
{
  "storyKey": "FEATURE-001",
  "parentKey": null,
  "subType": null,
  "subs": [
    {"storyKey": "FEATURE-001-sub-1", "subType": "bug-fix", "status": "completed"},
    {"storyKey": "FEATURE-001-sub-2", "subType": "refinement", "status": "active"}
  ],
  ...
}
```

## 8. TUI 交互设计

### 8.1 创建入口

在故事详情面板（Detail Panel）中新增操作：

```
┌─ FEATURE-001 详情 ──────────────────────────┐
│ 标题: 用户登录功能                             │
│ 阶段: implement ●                             │
│ 状态: active                                  │
│                                               │
│ [n] 新建子故事    [p] 暂停    [d] 删除         │
└───────────────────────────────────────────────┘
```

按 `n` 弹出创建对话框：

```
┌─ 创建子故事 ─────────────────────────────────┐
│                                               │
│ 类型:                                         │
│  > 缺陷修复 (bug-fix)                         │
│    联调适配 (integration)                      │
│    需求补充 (refinement)                       │
│    返工重做 (redo)                             │
│    [自定义输入...]                             │
│                                               │
│ 起始阶段: implement  (自动根据类型推导)         │
│                                               │
│ 描述: 修复以下问题：____________________       │
│       (模板已根据类型预填充)                    │
│                                               │
│            [确认]    [取消]                     │
└───────────────────────────────────────────────┘
```

交互流程：
1. 选择类型（预设 or 自定义输入），从配置加载
2. 起始阶段自动推导，用户可修改
3. 描述字段根据 `description_template` 预填充
4. 确认创建

### 8.2 故事列表展示

默认折叠，只显示子故事计数；展开时缩进显示：

```
# 折叠态
FEATURE-001  用户登录功能        [design→implement→test]  ◉ implement
                                  └─ 2 个子故事 [展开]

# 展开态
FEATURE-001  用户登录功能        [design→implement→test]
                                  ◉ implement
├─ FEATURE-001-sub-1  [bug-fix]  修复登录后空白  [implement→test]
│                                    ◉ test
└─ FEATURE-001-sub-2  [redo]     重做登录逻辑    [design→implement→test]
                                     ◉ design
```

快捷键：在父故事卡片上按 `Enter` 或 `→` 展开/收起子故事。

### 8.3 类型 Badge 颜色

颜色从配置读取，默认值：

| 类型 | 颜色 |
|------|------|
| bug-fix | 红色 |
| integration | 黄色 |
| refinement | 蓝色 |
| redo | 橙色 |
| 自定义 | 灰色 |

### 8.4 中止子故事

在子故事详情面板中增加中止操作：

```
┌─ FEATURE-001-sub-1 详情 ─────────────────────┐
│ 标题: 修复登录后空白                           │
│ 类型: bug-fix                                 │
│ 阶段: test ◉                                  │
│ 状态: active                                  │
│                                               │
│ [a] 中止    [p] 暂停    [d] 删除               │
└───────────────────────────────────────────────┘
```

## 9. Prompt 注入

子故事起始阶段的 prompt 需要注入额外上下文，让 AI CLI 知道这是一个子任务。

### 9.1 注入模板

在渲染 prompt 时，如果是子故事，在 prompt 头部注入：

```markdown
## 子任务上下文

- **父故事**: {parent_key} — {parent_title}
- **类型**: {sub_type}
- **任务描述**: {description}

请基于以下已有成果完成本任务：
- PRD: {prd_path}
- 设计文档: {spec_path}
- 上次评审: {review_summary}
```

### 9.2 不同类型的 prompt 侧重点

从配置的 `description_template` 生成，各类型默认值：

| 类型 | 注入内容 |
|------|---------|
| bug-fix | "修复以下问题：{description}"，附带 implement 阶段的代码上下文 |
| integration | "前后端联调修改：{description}"，附带接口文档和当前实现 |
| refinement | "需求补充/调整：{description}"，附带现有 design 文档 |
| redo | "重做，原因：{description}"，附带被否决的旧方案和评审意见 |

## 10. 父子状态联动

### 10.1 创建子故事时

```
父故事 status: active → waiting_subtasks
```

父故事暂停执行，等待子故事进入 terminal 状态。

### 10.2 子故事状态变更时

每次子故事状态变更（完成/中止/失败），触发父故事恢复检查：

```python
def check_parent_resume(parent_key: str):
    subs = db.get_sub_stories(parent_key)
    TERMINAL_STATUSES = {"completed", "aborted", "failed"}
    SUCCESSFUL_STATUSES = {"completed"}

    if not all(s["status"] in TERMINAL_STATUSES for s in subs):
        return

    # 所有子故事已结束，但不一定都成功
    summary = build_sub_summary(parent_key, subs)
    db.update_context(parent_key, "sub_story_results", json.dumps(summary))

    has_unsuccessful = any(s["status"] not in SUCCESSFUL_STATUSES for s in subs)
    if has_unsuccessful:
        db.update_context(parent_key, "sub_story_has_unsuccessful", "true")
        db.update_story(parent_key, status="paused", last_error="存在未成功的子故事，请人工确认")
        db.log_stage(parent_key, "", "pause", "子故事已结束但存在 aborted/failed，需要人工确认")
        return

    db.update_story(parent_key, status="active")
    start_story_async(parent_key)

def build_sub_summary(parent_key: str, subs: list) -> dict:
    return {
        "total": len(subs),
        "completed": [
            {
                "story_key": s["story_key"],
                "type": s["sub_type"],
                "description": s["title"],
            }
            for s in subs if s["status"] == "completed"
        ],
        "aborted": [
            {"story_key": s["story_key"], "type": s["sub_type"]}
            for s in subs if s["status"] == "aborted"
        ],
        "failed": [
            {"story_key": s["story_key"], "type": s["sub_type"]}
            for s in subs if s["status"] == "failed"
        ],
        "has_unsuccessful": any(s["status"] != "completed" for s in subs),
    }
```

### 10.3 状态流转图

```
父故事 (active)
  │
  ├─ 创建子故事
  │  父故事 → waiting_subtasks
  │  子故事 → active (获取 workspace 锁，开始执行)
  │
  ├─ 子故事执行中...
  │
  ├─ 子故事完成 (completed)
  │  释放 workspace 锁
  │  检查兄弟 → 还有未完成的? 父故事保持 waiting_subtasks
  │
  ├─ 子故事中止 (aborted)
  │  释放 workspace 锁
  │  aborted 是 terminal 但不是 successful
  │  如果仍有未 terminal 子故事，父故事保持 waiting_subtasks
  │  如果所有子故事都 terminal，父故事 → paused，等待人工确认或 review
  │
  ├─ 子故事失败 (blocked)
  │  释放 workspace 锁
  │  父故事保持 waiting_subtasks
  │  用户可手动处理：重试/中止/标记 failed
  │
  ├─ 最后一个子故事结束
  │  全部 successful: 父故事 → active
  │  存在 unsuccessful: 父故事 → paused，带 sub_story_has_unsuccessful 标记
  │
  └─ 手动恢复父故事
     检查未完成子故事 → 提示暂停或中止 → 父故事 → active
```

### 10.4 异常处理

| 场景 | 处理 |
|------|------|
| 子故事失败 | 标记 `blocked`，父故事保持 `waiting_subtasks`，用户手动处理 |
| 所有子故事都 blocked | 父故事不会自动恢复，用户需手动恢复或中止子故事 |
| 手动恢复父故事 | 强制暂停或中止所有未完成子故事（TUI 提示选择策略） |
| 子故事中止 | `aborted` 是 terminal 但 unsuccessful；全部结束后父故事进入 `paused`，等待人工确认或 review |
| 嵌套创建 | 代码层拦截：`parent_key` 指向的 story 如果自己也有 `parent_key`，拒绝创建 |

## 11. 嵌套限制与未来扩展

P0 禁止嵌套，但代码层预留扩展点：

```python
MAX_SUB_DEPTH = 1  # P0 = 1（不允许嵌套），未来可调大

def create_sub_story(parent_key, ...):
    # 检查嵌套深度
    depth = 0
    current = db.get_story(parent_key)
    while current and current.get("parent_key"):
        depth += 1
        current = db.get_story(current["parent_key"])
    if depth >= MAX_SUB_DEPTH:
        raise BusinessError("超过最大子故事嵌套深度")
```

## 12. Story Key 命名规则

```
父故事: FEATURE-001
子故事: FEATURE-001-sub-1
子故事: FEATURE-001-sub-2
```

规则：`{parent_key}-sub-{index}`，index 从 1 开始递增。

## 13. 实现范围与优先级

### P0 — 核心功能 + 安全约束

1. DB 层：新增 `sub_type` 字段，并同步更新 `VALID_COLUMNS`、`create_story()`、`upsert_story()`、API response
2. Service 层：`create_sub_story()` 函数（含嵌套检查、context 大小控制、并发 key 冲突重试）
3. API 层：`POST /api/story/{parent_key}/sub` 端点
4. Context 继承：带大小阈值的深拷贝
5. 父子状态联动：waiting_subtasks → active/paused
6. **工作区互斥锁**：只保护 AI 执行阶段；同一 workspace 同时只有一个 story 进入 Claude/Codex/Aider 执行
7. **手动恢复安全检查**：恢复父故事时强制处理未完成子故事
8. **子故事中止**：`POST /api/story/{story_key}/abort`
9. **子故事成果汇总**：父故事恢复时写入 `sub_story_results`，存在 aborted/failed 时写入 `sub_story_has_unsuccessful`
10. **waiting_workspace 恢复**：服务启动和 watchdog 轮询时将遗留 `waiting_workspace` 重新纳入调度

### P1 — TUI + 基础增强

10. 故事详情面板：创建子故事操作
11. 创建对话框：类型选择 + 阶段选择 + 描述预填充
12. 故事列表：折叠/展开 + 缩进展示 + 类型 badge
13. 子故事详情面板：中止操作
14. **类型配置化**：从 config.yaml 加载类型定义
15. **子故事模板**：按类型预填充描述格式（bug-fix：问题描述→复现步骤→预期行为）

### P2 — Prompt 增强

16. 子故事 prompt 模板注入
17. 按类型区分 prompt 侧重点

### P3 — 扩展能力

18. 子故事批量创建
19. Git 分支策略（子故事在独立分支工作，完成后合并）
20. 冲突检测与可视化 diff
21. 嵌套子故事支持（调整 MAX_SUB_DEPTH）

## 14. 风险与约束

| 风险 | 缓解措施 |
|------|---------|
| 共享工作区并发冲突 | P0 实现执行阶段互斥锁，同一 workspace 同时只有一个 story 进入 AI CLI 执行 |
| 手动恢复导致父子同时执行 | 调度恢复不拿 workspace 锁；后续 AI 执行阶段统一排队拿锁 |
| 子故事无法撤销 | P0 支持 abort 操作；`aborted` 是 terminal 但 unsuccessful |
| 强制中止后 workspace 锁未释放 | `abort_story()` / `stop_story_execution()` 必须在 `finally` 中释放执行阶段锁 |
| 子故事 context 与父故事不同步 | context 创建时深拷贝，之后独立 |
| context_json 过大 | P0 设置 1MB 阈值，超过只继承索引信息 |
| 子故事命名冲突 | parent_key 前缀 + 递增 index，DB UNIQUE 约束 + 代码层重试 |
| 多进程部署锁失效 | P0 明确只支持单进程 `story serve`；P1 考虑文件锁或 DB 锁 |
| 服务重启后 waiting_workspace 卡住 | 启动恢复和 watchdog 将 `waiting_workspace` 重新置入调度 |
| 嵌套子故事复杂度爆炸 | P0 禁止嵌套（MAX_SUB_DEPTH=1），代码层拦截 |
| waiting_subtasks 死锁 | 手动恢复入口 + TUI 提示选择策略；所有子故事 terminal 后父故事脱离等待 |

## 15. 验收标准

1. 能从父故事创建子故事，指定类型和起始阶段
2. 子故事自动继承 workspace、profile、context_json
3. 父故事进入 waiting_subtasks，子故事独立执行
4. 子故事全部 terminal 后父故事脱离 `waiting_subtasks`，context 包含子故事成果汇总
5. **同一 workspace 同一时刻最多一个 story 进入 AI CLI 执行阶段**（互斥保证）
6. **手动恢复父故事时，未完成子故事被强制暂停或中止**
7. **支持中止子故事（abort）；`aborted` 是 terminal 但 unsuccessful，父故事需进入人工确认或 review**
8. **context_json 超过 1MB 时，子故事只继承索引信息**
9. **子故事不能嵌套创建（代码层拦截）**
10. **`waiting_workspace` 状态可恢复：服务重启或 watchdog 轮询后不会永久卡住**
11. TUI 中子故事折叠显示计数，可展开查看详情；`waiting_workspace` 有明确展示状态
12. 子故事 prompt 包含父故事上下文信息
13. 类型定义从 config.yaml 加载，支持自定义扩展
