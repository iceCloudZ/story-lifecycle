# Workspace Entity Design — 工作区实体化 + Wiki + 多源探测

> 把 story-lifecycle 的"workspace"从物理路径升级为**业务项目实体**：一个 Workspace 代表一个业务项目（如 HappyCash 授信域），聚合仓库、story、wiki、测试旅程和外部集成（GitLab/CI/测试框架）。
> 本文档自包含——执行者无需访问 hc-pytest / hc-all 仓库即可完成设计理解；hc 侧资产仅作参考实现盘点（§7）。

## 背景与核心决策

### 问题

当前"workspace"一词在 story-lifecycle 里有三个含义，且都不是"项目"：

1. **intake 主工作区**——story 创建时记的目录（`story.workspace` 列）
2. **per-story 隔离目录**——`<worktrees_root>/<slug>/`，规划 LLM 起 slug，后端 mkdir，存 `context_json.workspace_path`（`planner.py:398-483`），本质是**代码改动的沙箱**
3. **`project.repo_path`**——`infra/db/models.py:316` 的 `project` 表是**仓库登记簿**（name/repo_path/availability），一个 project = 一个 git 仓库

缺的是**业务项目层**：HappyCash 授信域这种跨多个后端仓库（hc-credit / hc-risk / hc-order…）的项目，没有一个实体承载它的知识（wiki）、测试旅程（journey）、外部集成（GitLab/CI）。知识层（`.story/knowledge/` 的 scenario/playbook/failure）已有 wiki 的胚胎机制，但只服务 agent（prompt 注入），没有人的阅读界面，也没有项目级的家。

### 核心决策：加一层 Workspace 实体，四层模型

```
Workspace（业务项目）        ← 新顶层：wiki + 旅程 + 集成 + stories 的家
  └── Repo（仓库）           ← 现有 project 表,降为 Workspace 的组成部分(1:n)
        └── Story（需求）     ← 现有,通过 story_project 绑定到 Repo
              └── Sandbox    ← 现 workspace_path 改名,per-story 物理沙箱
```

**术语一次性理清（硬约定，写代码和文档都遵守）：**

| 术语 | 含义 | 旧称 |
|---|---|---|
| **Workspace** | 业务项目实体（新顶层） | —（新概念） |
| **Repo** | git 仓库登记（现有 `project` 表） | project |
| **Sandbox** | per-story 物理隔离目录 `<worktrees_root>/<slug>/` | workspace_path / 工作空间 |
| intake workspace | story 创建时记的主目录（保留旧名，不再单独讨论） | 主工作区 |

**业界先例**（本设计借鉴的三条路线）：

- **Backstage System Model**——Component（单个软件）之上聚合 System → Domain（业务域）；实体描述跟代码走，catalog 是投影层。→ 我们的 Workspace:Repo = System:Component。
- **Backstage Scaffolder**——新建项目是**模板化执行的声明式 step 序列**，不是手工配置。→ §3 初始化管线。
- **DeepWiki（Cognition）**——wiki 可从代码自动生成；agent 消费接口克制（读目录索引 / 读单页 / 提问）。→ §4 双读者设计。
- **Swimm**——文档锚定代码（Smart Tokens），PR 时 Auto-sync 检测漂移：小改自动修、大改交人 review。→ stale 检测比对 git 语义不比 mtime；draft→review 管线的正确性被验证。

### 关键决策清单

| # | 决策 | 理由 |
|---|---|---|
| D1 | Workspace : Repo = **1 : n** | 业务项目跨多个后端仓库（HappyCash 形态） |
| D2 | 初始化用**独立 pipeline**，不复用 story 引擎 | init 状态简单、步骤固定；塞 story 会污染需求生命周期统计 |
| D3 | wiki 是知识层的**新条目类型**（`type: wiki`），不是独立存储 | 不建第二个知识库；复用版本化/stale/review 全部机制 |
| D4 | wiki **人优先、agent 读摘要** | 详见 §4；wiki 是知识层唯一"人优先"的类型 |
| D5 | 探测源分层 L1-L4，hc 专用探测走**第六条缝**（BaseWikiProbe） | 核心保持通用开源，同 verify_provider 模式 |
| D6 | 测试旅程展示 = scenario 条目的 **UI 投影**，不存 wiki 页 | 结构化数据源直接投影，无同步问题 |
| D7 | GitLab/CI 初期只做**元数据登记 + 状态展示** | 不自建 CI 编排（Backstage 级投入，沾不起） |
| D8 | `workspace_path` 字符串语义**不动** | 15+ 消费点（spawn cwd / artifact 扫描 / diff）零改动，概念重命名即可 |

---

## 1. 实体模型

### 1.1 workspace 表（新增）

```sql
CREATE TABLE IF NOT EXISTS workspace (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,           -- 业务项目名,如 "HappyCash 授信域"
    slug TEXT NOT NULL UNIQUE,           -- kebab-case,用于目录/URL
    knowledge_root TEXT,                 -- 知识根目录(默认 <主工作区>/.story/knowledge)
    integrations_json TEXT NOT NULL DEFAULT '{}',  -- §6 集成元数据
    init_state TEXT NOT NULL DEFAULT '{}',         -- §3 初始化管线状态(每步 done/pending)
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
```

### 1.2 project 表加 workspace_id（现有表扩展）

```sql
ALTER TABLE project ADD COLUMN workspace_id INTEGER REFERENCES workspace(id);
-- NULL = 未归属任何 Workspace 的散仓库(向后兼容,开源单仓库用户可以不建 Workspace)
```

### 1.3 不变的关系

- `story_project`（story↔repo n:m，带 branch/worktree_path/worktree_state）**不动**——story 仍然绑定到 Repo 层，不直接绑 Workspace
- `context_json.workspace_path`（Sandbox 路径字符串）**不动**——只是文档和 prompt 里改叫 Sandbox
- `.story/knowledge/` 物理位置**不动**——概念上归属权从"主工作区"改为 Workspace（`workspace.knowledge_root` 记录它）

### 1.4 开源零配置路径

不配任何 Workspace 时行为与今天完全一致：单仓库用户 = 一个隐式 Repo、story 直接跑、知识层照旧。Workspace 是**显式 opt-in** 的实体，同 `verify_provider` 默认 None 的哲学。

---

## 2. WorkspacePage（前端）

新的顶层路由，与 Board 平级。Phase 2 只读，Phase 3 加 review 交互。

| Tab | 内容 | 数据来源 |
|---|---|---|
| **Wiki** | wiki 条目渲染（markdown + 锚点目录 + 交叉链接）；draft 待审标记 | 知识层 `type: wiki` 条目 |
| **旅程** | journey 名、test_ref、last_status、最近运行时间、PASS/FAIL 徽章 | scenario 条目投影（**不是 wiki 页**，D6） |
| **Stories** | 该 Workspace 下所有 story（经 Repo → story_project 反查） | 现有表 |
| **概览** | Repo 列表 + availability、runtime facts、集成状态（GitLab/CI 绿红） | project / project_runtime_fact / integrations |

注意 `frontend/AGENTS.md` 的 SVG 图标规则——tab 图标不用 emoji。

---

## 3. Workspace 初始化管线

借鉴 Backstage Scaffolder：**声明式 step 序列，顺序执行、每步幂等、产 draft、人确认**。状态存 `workspace.init_state`（`{"register_repos": "done", "gen_wiki": "pending", ...}`），中断可续跑。**独立 pipeline，不走 story 引擎**（D2）。

```text
story workspace init <name>
  ├─ step 1  register_repos    注册 Repo(repo_path/默认分支)→ project 表加 workspace_id
  ├─ step 2  detect_runtime    探测运行时事实 → project_runtime_fact(现有机制,自动跑)
  ├─ step 3  gen_wiki          L1 代码扫描生成 wiki 骨架 → L2-L4 probe 增补 → 全部 draft(§4/§5)
  ├─ step 4  register_integrations  登记 GitLab/CI/测试框架元数据(§6)
  └─ step 5  init_scenarios    跑现有 scenario 知识生成,旅程目录就位
```

- 每步失败不阻塞后续步骤（标记 `failed` + 原因，可单独重跑 `story workspace init --step gen_wiki`）
- step 3 的 draft 在 WorkspacePage Wiki tab 等待人确认——**初始化完成 ≠ wiki 生效**，生效永远经过人（不变量 I2）
- 哪个 probe 没配就缺哪层，优雅降级（开源用户只有 L1 骨架，照样可用）

---

## 4. Wiki 条目设计（双读者：人优先 + agent 读摘要）

### 4.1 schema（`type: wiki`，知识层新条目类型）

```yaml
---
id: wiki:hc-credit-domain
type: wiki
title: 授信域概述
summary: |            # ≤200 字,agent 注入只用这段
  授信域覆盖申请→风控→额度→动支,核心服务 hc-credit/hc-risk,
  关键状态机 RISK_RESULT...
source: human         # human | story:<key> | probe:<name>
evidence_refs:        # 论断的证据链(§5.3),AI/probe 产出时必填
  - {probe: dms_schema, query: "t_loan_order status 分布", observed_at: "..."}
source_refs: [...]    # 关联代码文件,喂 stale 检测
related: [scenario:borrow-flow, playbook:risk-timeout]
updated_at: ...
verified_at: ...      # 人工确认时间
---
（正文：给人看的完整叙述——架构图、演进历史、分歧记录、踩坑记录）
```

同步扩展：`packages/knowledge/src/knowledge/models.py` 加 `WikiEntry`，`parser.py` 支持解析，INDEX.json 索引。版本化复用现有知识条目机制。

### 4.2 双读者消费方式

**人（优先）**：WorkspacePage Wiki tab 渲染全文；`related` 渲染成可点交叉链接（wiki ↔ scenario/playbook 互跳——这是 wiki 区别于文档堆的地方）。

**agent**：

- prompt 注入**只取 `summary` + `related` 指针**，不注入全文（token 稀缺；需要细节时执行 agent 自己 read_file）
- 检索**降权**：wiki 是二手知识（从代码/scenario/实践综合），权低于一手条目（scenario/playbook/failure）——防止 agent 拿综述跳过细节，综述 stale 时是体面的错误
- stale 的 wiki 注入时**必须标注**（"此页可能过期，以代码为准"）

### 4.3 人写 vs AI 写

| 来源 | 流程 | 约束 |
|---|---|---|
| **人写**（`source: human`） | 直接生效，不进 draft 管线 | 人在自己 wiki 写字不需要自审 |
| **story 产出**（`source: story:<key>`） | **必须** draft → lint → review → 人工确认 → merge | draft 强制带证据链（哪个 story、哪个 artifact、哪次 journey 运行） |
| **probe 产出**（`source: probe:<name>`） | 同上，draft 管线 | evidence_refs 带 probe 查询和观测时间 |

### 4.4 story 推进 → wiki 更新的触发点

| 触发 | 产出 | 自动度 |
|---|---|---|
| design 阶段 spec.md 落地 | 相关 wiki 页的 draft 更新 | draft，待人审 |
| verify 通过 / story 完成 | journey `last_status` 回写（事实，非叙述） | **自动**（scenario 条目字段更新，不走 draft） |
| story 交付 | 涉及的 wiki 页 stale 重估 | 自动检测，更新仍走 draft |

**闭环语义**：story 推进产生的不只是代码，还有**待消化的知识**。WorkspacePage 因此有第二职能：**review 收件箱**——这是 flywheel 在项目层的形态。

---

## 5. 多源探测模型（BaseWikiProbe — 第六条缝）

### 5.1 按"离真相的距离"分层

```text
L1 代码扫描       意图层   服务、API、MQ tag、表结构定义       → 核心自带(开源开箱即用)
L2 配置/部署      拓扑层   Nacos 配置、依赖关系、环境拓扑       → 专用 probe
L3 数据现实       状态层   生产库 schema + 枚举分布 + 数据量     → 专用 probe
L4 行为现实       流量层   行为日志、ES 请求、MQ 消费            → 专用 probe
```

wiki 的价值主要在 L3/L4——它们能**证伪** L1：代码声称的和线上跑的不一致时，wiki 写现实、标注分歧（"代码定义 5 态，线上存在 8 态，X/Y 为历史遗留"）。**分歧记录是最有价值的 wiki 内容。**

### 5.2 缝的契约（与五个先例同构）

先例：context_provider / source / adapter / verify_provider（文档 10）/ knowledge Provider。第六条：

```python
# packages/story-lifecycle/src/story_lifecycle/knowledge/wiki_probes/base.py
"""wiki 探测源契约。核心不硬依赖任何特定数据源(DMS/SLS/ES/Mongo)。
失败返回 [] 不阻断(同 BaseStoryContextProvider 容错哲学)。"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

@dataclass
class Evidence:
    layer: str                    # L1 | L2 | L3 | L4
    kind: str                     # table_distribution | api_traffic | ...
    summary: str                  # 人读的一句话结论
    data: dict = field(default_factory=dict)   # 聚合统计(绝无原始行,见 I5)
    query: str = ""               # 产生此证据的查询(审计 + 复跑)
    observed_at: str = ""

class BaseWikiProbe(ABC):
    def __init__(self, config: dict):
        self.config = config

    @abstractmethod
    def probe(self, workspace: dict) -> list[Evidence]:
        """执行探测,返回证据列表。异常/未配置 → 返回 [],不抛。"""
        ...
```

加载器 mirror `verify_providers/__init__.py`：config `wiki_probes: [{module, class, path?, ...}]` 驱动 importlib，加载失败 print + 跳过。

### 5.3 证据链与 stale 升级

- 每条 wiki 论断带 `evidence_refs: [{probe, query, observed_at}]`——人 review draft 时能看到结论来源
- stale 检测升级：**重跑 probe 对比**（enum 分布变了 → L3 证据过期）+ git 语义比对（`git log -1 --format=%ct -- <file>` vs `verified_at`）。**不用文件 mtime**（checkout/touch 都刷 mtime，误报高）

### 5.4 核心只带 L1

开源核心只实现 `CodeScanProbe`（静态扫 API 注解/表定义/依赖文件）。DMS/SLS/ES/Mongo 等专用 probe 全部在 hc 侧仓库（§7 已有现成底座），不进本仓库。

---

## 6. 集成元数据（GitLab / CI / 测试框架）

存 `workspace.integrations_json`：

```json
{
  "gitlab":   {"url": "...", "project_ids": {"hc-credit": 123}},
  "ci":       {"provider": "gitlab-ci", "status_source": "pipeline_api"},
  "verify":   {"provider_config_key": "verify_provider"},
  "probes":   {"dms_instance": "印尼公共实例", "es_logstore": "..."}
}
```

**边界（D7）**：初期只做两件事——story 卡片跳 MR 链接、概览 tab 显示 pipeline 绿红。**不自建 CI 编排、不接 webhook 驱动流程**。这些元数据同时是 probe 和 verify_provider 的 config 来源（一处登记，多处消费）。

---

## 7. 参考实现现状盘点（hc-pytest / hc-all，2026-08-03 探查）

hc 侧已有资产比预想厚——**L4 探测底座已存在，不要重复造**：

| 资产 | 位置 | 对本设计的意义 |
|---|---|---|
| ES `EndOfRequest` 采集 | `hc-pytest/data_sources/es_loader.py` | 现成的 **L4 probe 底座**（req+resp+traceId 全量），薄封装成 `EsEndOfRequestProbe` |
| MongoDB 行为事件采集 | `data_sources/behavior_loader.py` | L4 第二源（`hc-event-tracking` page/click/business） |
| PII 参数化脱敏 | `data_sources/normalizer.py` | I5 红线已在采集层落地，probe 直接继承 |
| 多用户轨迹场景挖掘 | `reconstructor/pattern_miner.py` | "代码意图 vs 流量现实"**分歧页的自动原料**（稳定模板+分支变体） |
| 业务知识硬编码表 | `generator/builder.py` `SCENE_API_MAP` | 唯一的业务知识载体，引用仓库外的 `CONTEXT.md`/`data-map.md` |
| journey 元数据 | `journeys/*.yaml`（name/description/tags/stage 链） | 旅程 tab 的现成数据源；**缺口：无显式服务/API 清单**，需从 stage 的 `gateway`+`path` 聚合提取 |
| DB/MQ 外部代理 | `conftest.yaml` 的 `cli_sql_path`/`cli_mq_path` | 只读治理通道约束已在架构里（probe 不直连库） |

**缺口**：无 README、无 wiki 资产、无 integrations/ 代码（`HcPytestVerifyProvider` 未写）；`data-map.md`/12 库 schema 在仓库外——wiki 生成时需决策这些口径文档是复制进知识层还是引用。

---

## 8. 分期

| Phase | 内容 | 验收 |
|---|---|---|
| **1** 测试框架接入 | 按文档 10 执行（采纳异步产物模式：provider 起跑测试即返回，journey 跑完 declare `scenario_report` 到 `<sandbox>/story/`，gate 下轮读证据；外部 FAIL 计 reject budget 防死循环） | 文档 10 验收清单 |
| **2** Workspace 实体化 | `workspace` 表 + `project.workspace_id` + 术语切换（prompt/文档里 workspace_path→Sandbox）+ 初始化管线 5 步 + WorkspacePage 只读版（旅程/Stories/概览 tab） | 见 §9 |
| **3** Wiki | `WikiEntry` + L1 CodeScanProbe + hc probes（薄封装 data_sources/）+ wiki draft 管线 + Wiki tab + review 收件箱 | 见 §9 |

每 Phase 独立可验收；Phase 1 不动 Phase 2/3 地基（`workspace_path` 语义不变）。

---

## 9. 验收标准

**Phase 2**：

- [ ] 不建 Workspace 时行为与今天完全一致（开源零配置路径）
- [ ] `story workspace init hc-credit-domain` 5 步可跑、幂等、单步可重跑；init_state 正确推进
- [ ] WorkspacePage 三 tab（旅程/Stories/概览）数据正确；旅程 tab 显示 scenario 的 last_status
- [ ] 代码/文档/prompt 中 per-story 目录统称 Sandbox，无"workspace"三义残留

**Phase 3**：

- [ ] `type: wiki` 条目可解析、进 INDEX.json；人写的直接生效，AI/probe 产出的一律 draft
- [ ] agent prompt 注入只含 summary + related，且 wiki 条目检索权低于 scenario/playbook；stale 标注生效
- [ ] 不配 probe 时只有 L1 骨架；配了 DMS/ES probe 后 wiki draft 含 L3/L4 证据（带 evidence_refs）
- [ ] 分歧记录可用：代码定义与 probe 观测不一致时，wiki draft 写现实 + 标注分歧
- [ ] stale 检测支持"重跑 probe 对比"，无 mtime 误报
- [ ] PII 审计：wiki 条目和 prompt 注入中无原始用户数据（只有聚合统计）

---

## 10. 不变量（执行时不能破坏）

- **I1 通用性**：核心不硬依赖 HappyCash 特定代码/数据源（hc_order/DMS/SLS 等全在 hc 侧 probe 和 provider 里）
- **I2 AI 不自动覆盖正式知识**：story/probe 产出永远 draft → review → 人工确认 → merge；自动的只有事实字段回写（journey last_status）
- **I3 新缝同构**：BaseWikiProbe 遵循先例模式——ABC + config importlib 加载 + 失败不阻断 + 默认不配零影响
- **I4 probe 只读**：生产访问走已有治理通道（DMS/外部 CLI 代理），probe 不引入新写路径；probe 失败 = 该层证据缺失，优雅降级
- **I5 PII 红线**：probe 只产聚合统计（计数/分布/比例）；原始用户数据永不进 wiki 条目、永不进 prompt 注入
- **I6 不建第二知识库**：wiki 是知识层条目类型，复用版本化/stale/review 机制；旅程展示是 scenario 投影，不另存
- **I7 workspace_path 不动**：字符串语义和 15+ 消费点零改动，只改术语

---

## 关联文档

- `10-test-framework-integration-design.md` — Phase 1 的完整设计（BaseVerifyProvider 缝、scenario_catalog 注入、scenario 知识闭环）；本文档是其上层实体化演进
- `07-scenario-knowledge-workflow-design.md` — Scenario Knowledge Layer 总设计（旅程 tab 的数据源）
- `08-init-knowledge-interaction-design.md` — `init-knowledge` 交互设计（§3 step 3 的 L1 骨架生成复用其交互模式：扫描概览 → 范围确认 → 生成）
- hc-pytest 侧架构决策：`D:/hc-all/hc-pytest/docs/plans/2026-08-03-golden-data-generator-and-story-lifecycle-integration.md`（金数据采集 = L4 probe 底座，§7）
- 业界先例：Backstage System Model / Scaffolder、DeepWiki（Cognition）、Swimm Auto-sync
