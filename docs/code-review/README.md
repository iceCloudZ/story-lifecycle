# 并行代码库审计方案 · 总文档

**4 个 AI 并行审计当前 `main` 分支上已经存在的全部代码**，按关注维度切分、关注点零重叠。这是对**整个代码库的现状**做体检，不是审查某次 PR 的 diff。

```
docs/code-review/
├── README.md                          ← 你在这里（总文档：流程、合并协议、判定规则）
├── ai-1-correctness-security.md       ← AI-1 的可执行 prompt（功能 & 安全）
├── ai-2-architecture-layering.md      ← AI-2 的可执行 prompt（架构 & 分层）
├── ai-3-readability-style.md          ← AI-3 的可执行 prompt（可读性 & 风格）
└── ai-4-testing-performance.md        ← AI-4 的可执行 prompt（测试 & 性能）
```

每个子文档都是**独立可执行**的长程任务：把它作为 system prompt 喂给一个 AI agent，它就能独立完成对自己负责维度的全量扫描，不依赖其他子文档、不依赖总文档。

---

## 1. 任务性质：代码库审计 ≠ PR review

| | 代码库审计（本方案） | PR review |
|---|---|---|
| **输入** | main 分支上**全部代码**（4 个包的所有 .py 文件） | 一次提交的 diff |
| **范围** | 整个 repo 现状 | 增量改动 |
| **目标** | 找出**已存在**的问题、技术债、违例 | 阻挡有问题的改动合入 |
| **扫描方式** | 逐目录/逐模块全扫，分类归档 | 逐文件查 diff |

本方案针对**代码库审计**：假设 main 上的代码已经存在、可能积累了不少问题，要做一次彻底体检。

---

## 2. 为什么按维度切（而不是按包）

| 切分方式 | 适用场景 | 本 repo 的问题 |
|---|---|---|
| **按维度切**（本方案） | 任意范围 | ✅ 4 路并行、零冗余；每个 AI 是该维度的专家 |
| 按包切 | 一次改 4 个包的大型重构 | ❌ 单维度问题跨包分布，按包切会漏（比如安全问题的 pattern 在 4 个包里都有） |
| 按风险层级切 | 只关心致命问题 | ❌ 维度之间偶有交叉，去重困难 |

按维度切的好处：每个 AI 可以**跨包扫描同一类问题**（比如 AI-1 在所有 4 个包里找 SQL 注入 pattern），覆盖最全。

---

## 3. 工作流总览

```
                   ┌──────────────────────┐
   main 全量代码 ──▶│  分发到 4 个 AI 并行  │
                   └──────────┬───────────┘
        ┌──────────────┬──────┴───────┬──────────────┐
        ▼              ▼              ▼              ▼
   AI-1 功能&安全  AI-2 架构&分层  AI-3 可读性&风格  AI-4 测试&性能
   (子文档1)       (子文档2)       (子文档3)        (子文档4)
        │              │              │              │
        └──────────────┴──────┬───────┴──────────────┘
                             ▼
                    ┌────────────────────┐
                    │  合并 → 去重 → 排序 │  ← 人或聚合脚本
                    └────────┬───────────┘
                             ▼
                     统一审计报告
```

- **输入**：main 分支上 4 个包的全部源码
- **并行**：4 个 AI 互相不通信、不依赖彼此输出
- **输出**：每个 AI 吐一份结构化 JSON（schema 见 §4），按问题维度汇总
- **合并**：把 4 份 JSON 拼到一起，按 severity 去重排序（见 §5）

### 3.1 扫描范围约定

每个 AI 都扫这 4 个包的源码（不含 venv / dist / 缓存）：

```
packages/story-lifecycle/src/       ← 5 层架构：entry/sourcing/orchestrator/knowledge/infra
packages/story-lifecycle/tests/
packages/story-miner/miner/         ← flat layout
packages/story-miner/tests/
packages/knowledge/
packages/testing/
tests/                              ← root：跨包 contracts/integration/e2e
```

排除：`.venv*/`、`dist/`、`__pycache__`、`*.egg-info`、`ws/`、`*.db`、`.claude/`、`frontend/`（前端单独审）。

---

## 4. 统一输出 Schema（4 个 AI 共用）

每个 AI 输出一段 JSON。schema 如下，合并时可直接 `jq` 拼接、按 severity 排序、按 file+line 去重。

```json
{
  "reviewer": "AI-1",
  "focus": "correctness-and-security",
  "scan_scope": "packages/*/src/**/*.py",
  "stats": {
    "files_scanned": 87,
    "findings_count": 12
  },
  "findings": [
    {
      "severity": "blocking",
      "file": "packages/story-lifecycle/src/story_lifecycle/orchestrator/handlers.py",
      "line": 42,
      "category": "boundary",
      "title": "未处理 session 为 None 的情况",
      "detail": "delete_session() 在 session=None 时会直接抛 AttributeError，但调用方 entry/cli.py:88 没有空值检查。这是一个不可逆操作（删 session），崩溃在执行中途会留下脏状态。",
      "suggestion": "在 handler 入口加 if session is None: return None 并打印诊断日志，同时补一条回归测试覆盖 session=None 路径。"
    }
  ],
  "summary": "3 blocking, 5 warning, 4 nit"
}
```

**字段规则：**
- `severity`：`blocking`（必须修）/ `warning`（强烈建议）/ `nit`（无所谓）
- `category`：见 §5.3 枚举，便于聚合时归类
- `scan_scope`：报告本次扫了哪些路径，便于核查覆盖范围
- `stats`：扫了多少文件、找到多少问题，用于核查是否真的全扫了
- **不要为了凑数硬挑毛病**，找不到问题如实说 no findings

### 4.1 发现量控制（重要）

全量扫描可能产生几百条 finding。为避免报告失去焦点：
- **每个文件每个 category 最多记 3 条**（同 pattern 的重复问题合并成一条，detail 里列全部出现位置）
- **优先记 blocking 和 warning**，nit 只记特别典型的
- **同 pattern 的重复**：比如 10 个文件都有 N+1 查询，合成 1 条 finding，detail 列全部位置

---

## 4.2 同 pattern 去重示例

❌ 错误：记 10 条
```
finding 1: handlers.py:12 - N+1 查询
finding 2: handlers.py:45 - N+1 查询
finding 3: resolvers.py:88 - N+1 查询
...（共 10 条）
```

✅ 正确：合成 1 条
```
finding: "N+1 查询 pattern 在 orchestrator/ 多处出现"
detail: "出现位置：handlers.py:12, handlers.py:45, resolvers.py:88, ...（共 10 处）。
        根因是循环内调用 db.get_session()，应改为批量查询。"
suggestion: "在 orchestrator/ 引入 batch_get_sessions(sessions_ids) → dict，
            替换所有循环内单条查询调用点。"
```

---

## 5. 合并协议（4 份 JSON → 1 份审计报告）

### 5.1 聚合脚本

```bash
# 1. 把 4 份 JSON 存成 ai1.json ~ ai4.json
# 2. 合并 + 去重 + 按 severity 排序
jq -s '
  map(.findings[]
      | .reviewer = input_filename // "unknown")
  | group_by(.file + ":" + (.line|tostring) + ":" + .category)
  | map({
      file: .[0].file,
      line: .[0].line,
      category: .[0].category,
      severity: (map(.severity)
                 | sort_by({"blocking":0,"warning":1,"nit":2}[.])
                 | .[0]),
      title: .[0].title,
      detail: (map(.detail) | join(" / ")),
      suggestion: (map(.suggestion) | join(" / ")),
      flagged_by: (map(.reviewer))
    })
  | sort_by(.severity)
' ai1.json ai2.json ai3.json ai4.json > audit-report.json
```

### 5.2 去重规则

两个 finding 满足以下条件视为**同一问题**，合并：
- `file` 相同
- `line` 差距 ≤ 5
- `category` 相同

合并后：
- `severity` 取**更严重**的那个
- `flagged_by` 字段记录哪些 AI 都指出了它（多个 AI 同时命中 = 高置信度，优先处理）

### 5.3 `category` 枚举（跨 4 个 AI 互斥）

| AI | 可用 category |
|---|---|
| AI-1 | `logic` / `boundary` / `error-handling` / `security` / `irreversible` |
| AI-2 | `layering` / `state-model` / `side-effect` / `dead-branch` / `architecture-trigger` |
| AI-3 | `naming` / `comment-density` / `dead-code` / `chinese-content` / `artifact-leak` / `length` |
| AI-4 | `missing-test` / `missing-regression` / `error-path` / `n-plus-1` / `dependency` / `convention` |

> category 跨 AI 是**互斥**的（同一个 category 只属于一个 AI），所以去重时同 category 的命中必然来自同一类关注点，不会误并。

### 5.4 判定结论

聚合后看 `severity` 分布，给整个代码库一个健康度评级：

| 情况 | 健康度 | 建议动作 |
|---|---|---|
| 有任意 `blocking` | 🔴 **需立即修复** | blocking 项逐条修，修完才算合格 |
| 无 blocking，但有 `warning` | 🟡 **需排期修复** | warning 进入 backlog，下个迭代清 |
| 全是 `nit` 或 `no findings` | 🟢 **健康** | nit 按心情，不强制 |

---

## 6. 如何启动一个 AI 长程任务

每个子文档是一份完整的 system prompt。启动方式（任选）：

**方式 A：直接喂 system prompt**
```bash
# 把子文档内容作为 system prompt，扫描任务作为 user message
cat docs/code-review/ai-1-correctness-security.md   # 复制全部内容作为 system prompt
# user message 填: "扫描 main 分支上 packages/ 下所有 .py 文件，输出严格 JSON。"
```

**方式 B：让 AI agent 自己拉代码**
```
你的 system prompt 是 docs/code-review/ai-1-correctness-security.md 的全部内容。
现在执行审计：扫描当前 main 分支上 packages/ 下所有 .py 源码，
输出严格 JSON。
```

**并行启动 4 个**：开 4 个 agent 会话，分别喂 4 份子文档，同时跑。4 个任务互相独立、无依赖。

### 6.1 按需缩放扫描范围

如果代码库太大一次扫不完，可以：
- **按包分批**：先让 4 个 AI 都扫 `packages/story-lifecycle/`，下一轮再扫 `packages/story-miner/`，依此类推
- **按子目录分批**：AI-2 可以先扫 `entry/`+`sourcing/`，下一轮扫 `orchestrator/`+`knowledge/`+`infra/`
- 子文档里的"工作流程"一节给了具体的分批建议

---

## 7. 不适用场景

这套方案针对**全量代码库体检**。以下情况别硬套：

| 任务类型 | 建议方案 |
|---|---|
| PR review（审查增量改动） | 把"扫描范围"从全量代码改成 `git diff origin/main...HEAD` 即可，其余流程不变 |
| 紧急 hotfix 验证 | 只跑 AI-1（功能&安全）+ AI-4（测试）即可 |
| 单个 bug 深挖 | 直接让 AI-1 对该模块做深度分析，不需要 4 路并行 |
| 性能专项 | 只跑 AI-4，但把性能相关的检查项加细 |

---

## 8. 一句话总结

**main 全量代码，4 个 AI 各盯一个维度，输出统一 JSON，合并去重后按 severity 出健康度评级。**

- 关注点零重叠 → 4 路并行不会互相打架
- 统一 schema → 合并可脚本化
- severity 驱动 → 修复优先级看数据不看感觉
- 同 pattern 去重 → 避免几百条 finding 失去焦点

---

## 子文档索引

| 文档 | 维度 | 核心 category |
|---|---|---|
| [ai-1-correctness-security.md](./ai-1-correctness-security.md) | 功能正确性 & 安全 | `logic` `boundary` `security` |
| [ai-2-architecture-layering.md](./ai-2-architecture-layering.md) | 架构 & 分层 | `layering` `state-model` `side-effect` |
| [ai-3-readability-style.md](./ai-3-readability-style.md) | 可读性 & 风格 | `naming` `chinese-content` `artifact-leak` |
| [ai-4-testing-performance.md](./ai-4-testing-performance.md) | 测试 & 性能 | `missing-test` `missing-regression` `n-plus-1` |
