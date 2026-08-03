# Test Framework Integration Design

> 把外部专用测试框架（hc-pytest）接入 story-lifecycle 的 verify 门禁 + scenario 知识闭环。
> 本文档自包含——执行者无需访问 hc-pytest 仓库即可完成全部改动。
>
> **2026-08-03 修订**：经设计评审讨论，本文档已按修订意见更新（异步产物模式为主、retry 预算、去 mtime、跨仓解耦等），修订明细见文末「修订记录」。
>
> **实施状态（2026-08-03）**：Phase 1 已落地（commit `5f0c7b54`），实施结果与 5 处设计偏差见 `phases/PHASE-1-RESULT.md`；改动 5（hc-pytest 侧）待 hc 仓实施后真机联调。

## 背景与核心决策

### 问题

HappyCash（菲律宾消费金融平台）有一个专用测试框架 **hc-pytest**：Python YAML "journey" 场景测试，覆盖注册→资料→授信→借款→放款→还款全链路。它能测真实业务流（HTTP API + RocketMQ + DB 断言 + 风控 mock），是验证 story 改动是否破坏业务的关键手段。

当前 story-lifecycle 的 verify gate 是**纯 LLM 判定**（`unified_gate.py`），不调用任何外部测试。scenario 知识库（`.story/knowledge/scenarios/`）是**静态快照**——14 个 scenario 文件停在 2026-06-02，`apis` 字段全空，`sync-knowledge` 的 stale 检测是 TODO（`stale.py` 只比 git commit），规划 prompt 不注入 scenario。

### 核心决策：契约扩展点，不吸收

**hc-pytest 不吸收进 story-lifecycle。** 理由：

1. story-lifecycle 是通用开源引擎，`base.py:3-7` 明文规定"must NOT hard-depend on any specific external system"。hc-pytest 是 HappyCash 专用（`hc_order.t_loan_order`/`RISK_RESULT` MQ tag），吸收会污染通用性。
2. `packages/testing` 是 dogfood 工具（calculator/greeter toy 任务验证 AI 编排），跟 hc-pytest（测真实业务）是两个宇宙，强行合并破坏语义清晰度。
3. story-lifecycle 已有三个"外部专用能力通过契约注入"的先例，应加第四个对称的缝。

**三个先例**（都是同一模式：通用引擎定义窄 ABC + config 驱动 importlib 加载 + 核心不硬依赖）：

| 缝 | ABC | 加载位置 | config key |
|---|---|---|---|
| 知识注入 | `BaseStoryContextProvider` | `knowledge/context_providers/__init__.py:_load_provider` | `context_provider` |
| 需求来源 | `StorySource` | `sourcing/source_loader.py` | `source_type` |
| AI CLI | `BaseAdapter` | `knowledge/adapters/` | profile yaml |
| **测试验证（新）** | `BaseVerifyProvider` | `verify_providers/__init__.py`（新建） | `verify_provider` |

### 核心决策：异步产物模式为主，不在 gate 内同步跑测试（修订点 R1）

`run_unified_verify_gate` 跑在 planner poll loop 里，而 hc-pytest 全链路 journey（注册→放款→还款）分钟级起步——**同步调用会阻塞 driver 轮询循环，还可能触发 stuck 检测误报**。因此：

- **主路径（异步产物）**：provider 起跑测试后立即返回 `None`；journey 跑完由 hc-pytest 侧 `story tool declare scenario_report <path>` 落到 `<workspace>/story/` + `POST /api/story/{key}/gate-results`。artifact 落地后 `check_artifacts_landed` 自然看见，下一轮 gate 的 LLM 证据包自然带上 journey 结果。这复用仓库已确立的 artifact-driven 哲学（完成信号 = 产物落地）。
- **可选路径（同步冒烟）**：小冒烟集（秒级）可在 provider config 配 `sync: true`，`verify()` 同步返回 `VerifyResult`，gate 立即合并。

---

## 改动总览

共 5 块改动，按依赖顺序：

1. **新增 `BaseVerifyProvider` 扩展点**（核心）——让 verify gate 能调用外部测试框架
2. **规划注入 scenario_catalog**——让 planner LLM 选要测哪些场景
3. **board 加「测试场景」tab**——前端展示
4. **scenario 知识闭环**——填 apis + 绑 test_ref + 补 stale 检测
5. **hc-pytest 侧实现**（参考实现，不在本仓库）

改动 1-4 在本仓库，改动 5 在 hc-pytest 仓库（本文末尾给出契约，hc-pytest 侧照着实现）。

---

## 改动 1：新增 BaseVerifyProvider 扩展点

### 1.1 新建 `orchestrator/verify_providers/base.py`

```python
# packages/story-lifecycle/src/story_lifecycle/orchestrator/verify_providers/base.py
"""外部测试框架的验证契约。

story-lifecycle 是通用引擎，不硬依赖任何特定测试框架。
具体实现（如 hc-pytest 的 HcPytestVerifyProvider）通过 config 注入。
失败返回 None 不阻断 verify（同 BaseStoryContextProvider 的容错哲学）。
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class VerifyResult:
    """外部测试框架的验证结果。"""
    passed: bool = False
    summary: str = ""                           # 一句话总结
    findings: list[dict] = field(default_factory=list)   # [{scenario, status, detail}]
    evidence: dict = field(default_factory=dict)        # 任意结构化证据（journey pass/fail 明细）
    evidence_ref: str = ""                      # 报告路径等


class BaseVerifyProvider(ABC):
    """外部测试框架注入 verify gate 的契约。

    config 加载方式 mirror context_providers/__init__.py:_load_provider。
    默认 None（不配 verify_provider）→ 今天的 LLM-only gate，零行为变化。
    """

    def __init__(self, config: dict):
        self.config = config

    @abstractmethod
    def verify(self, story_key: str, workspace: str, stage: str,
               done_data: dict) -> VerifyResult | None:
        """执行外部测试，返回结果。返回 None 表示本轮不参与（降级到 LLM gate）。

        两种运行模式（修订点 R1）：
          - 异步产物（默认，推荐）：起跑测试后立即返回 None。测试跑完由
            provider 侧自行 declare scenario_report + POST gate-results，
            产物落地后下一轮 gate 自然带上证据。
          - 同步冒烟（config sync: true）：秒级测试集可同步执行并返回
            VerifyResult，gate 本轮立即合并。必须自行控制 timeout，
            不得阻塞 planner poll loop 超过 config 的 timeout_seconds。

        参数：
          story_key: story 标识
          workspace: 工作区路径（sandbox；异步模式下 provider 应把报告写进
                     <workspace>/story/，使 check_artifacts_landed 可见）
          stage: 当前阶段（通常 "verify"）
          done_data: verify stage 的 done.json 解析；**接线约定（修订点 R8）**：
                     gate 调用方负责把 ctx["_agent_actions"] 合入 done_data
                     （key 同名），provider 由此读规划时 LLM 选的
                     selected_scenarios
        """
        ...
```

### 1.2 新建 `orchestrator/verify_providers/__init__.py`

```python
# packages/story-lifecycle/src/story_lifecycle/orchestrator/verify_providers/__init__.py
"""config 驱动的 verify provider 加载（mirror context_providers）。"""
from __future__ import annotations
import importlib
from typing import Optional
from .base import BaseVerifyProvider


def load_verify_provider(config: dict) -> Optional[BaseVerifyProvider]:
    """从 config 加载 verify provider。未配置返回 None。

    修订点 R6：duck-type 校验（只要求有 verify() 方法），不强制
    issubclass(BaseVerifyProvider)——hc 侧实现因此不必在运行环境硬装
    story-lifecycle 包，跨仓依赖降为纯协议。
    """
    cfg = config.get("verify_provider")
    if not cfg:
        return None
    try:
        # 可选 sys.path prepend（加载非已安装包，如 hc-pytest 的 provider 入口）
        if cfg.get("path"):
            import sys
            p = cfg["path"]
            if p not in sys.path:
                sys.path.insert(0, p)
        module = importlib.import_module(cfg["module"])
        cls = getattr(module, cfg["class"])
        if not callable(getattr(cls, "verify", None)):
            raise TypeError(f"{cfg['class']} 缺少 verify() 方法（duck-type 校验）")
        return cls(config=cfg)
    except Exception as e:
        # 容错：加载失败不阻断，降级到 LLM-only gate
        print(f"[verify_provider] 加载失败，降级到 LLM gate: {e}")
        return None
```

### 1.3 接入 verify gate

**文件**：`orchestrator/evaluation/unified_gate.py`（`run_unified_verify_gate` 函数，`:61`）

在 LLM 判定完成后，追加调一次外部 verify provider，合并结果：

```python
# 在 run_unified_verify_gate 的 LLM 判定逻辑之后追加：

def _run_external_verify(story_key, workspace, done_data, context) -> Optional[VerifyResult]:
    """如果配了 verify_provider，执行（或起跑）外部测试。"""
    from orchestrator.verify_providers import load_verify_provider
    from infra.config import load_config  # 或现有的 config 读取方式
    config = load_config()
    provider = load_verify_provider(config)
    if provider is None:
        return None
    # 修订点 R8 接线：把规划产物 _agent_actions 合入 done_data，provider 据此读
    # selected_scenarios（selected_scenarios 存在 ctx["_agent_actions"]，不在
    # done.json 里——不合入 provider 将永远拿不到）
    done_data = {**done_data, "_agent_actions": context.get("_agent_actions", [])}
    try:
        return provider.verify(story_key, workspace, "verify", done_data)
    except Exception as e:
        print(f"[external verify] 执行失败，忽略: {e}")
        return None

# 在 gate decision 汇总处：
ext = _run_external_verify(story_key, workspace, done_data, context)
if ext is None:
    pass  # 异步模式（provider 起跑后返回 None）或未配置：本轮 LLM-only，无行为变化
else:
    if not ext.passed:
        # 外部测试失败 → 阻断 advance，转 retry（decision 枚举: advance/retry/fail）
        decision = "retry"
        repair_action = f"外部测试失败: {ext.summary}"
        # 修订点 R2：外部 FAIL 必须计入 reject budget（check_reject_budget，
        # ≤3 次/stage 且理由不得重复，否则 force-escalate）——环境挂/journey
        # 本身坏时，代码怎么改都过不了，不计预算会烧光 retry 陷入死循环
    # 外部 PASS 只合并 findings，**不得跳过人工 confirm-gate**（修订点 R3，
    # 同 boundary_judge 的 confirm=true 不变量：approve 不 auto-advance）
    for f in ext.findings:
        record_finding(story_key, category="test_failure", **f)
```

**single-pass profile 的挂接（修订点 R4）**：single-pass（handoff 包干）没有独立 verify stage，其完成判定同样经过 boundary_judge/unified_gate 这条路径——provider 在**该 profile 唯一阶段的完成判定点**被调用，语义不变：异步模式下 scenario_report 落到 `<workspace>/story/` 即证据，单阶段 profile 的三件 artifact 齐 + scenario_report 状态进入人工 confirm 视野。

### 1.4 config 格式（HappyCash 部署的 `~/.story-lifecycle/config.yaml`）

```yaml
# 默认不配 = LLM-only gate（开源用户零影响）
# HappyCash 部署才配：
verify_provider:
  module: "hc_pytest.integrations.story_lifecycle_provider"
  class: "HcPytestVerifyProvider"
  path: "D:/hc-all/hc-pytest"           # hc-pytest 仓库路径（加到 sys.path，仅加载 provider 入口模块）
  journeys_dir: "D:/hc-all/hc-pytest/journeys"
  sync: false                            # 修订点 R1：默认异步产物模式；true = 同步冒烟
  timeout_seconds: 120                   # 仅 sync: true 时生效，防阻塞 poll loop
```

---

## 改动 2：规划注入 scenario_catalog

让 planner LLM 在规划时看到候选 scenario，输出 `selected_scenarios`。

### 2.1 新增 `build_scenario_catalog_section`

**文件**：`orchestrator/engine/prompt_sections.py`（在现有 `build_knowledge_section` 旁，`:177`）

```python
def build_scenario_catalog_section(story_key: str, workspace: str, stage: str) -> str:
    """读 .story/knowledge 的 type=scenario 条目，渲染候选清单给规划 LLM 选。"""
    try:
        from knowledge.context_providers.knowledge_provider import _KNOWLEDGE_ROOT
        from knowledge.index import KnowledgeIndex
        idx = KnowledgeIndex(str(_KNOWLEDGE_ROOT))
        entries = idx.retrieve(story_key=story_key, workspace=workspace, stage=stage, top_k=20)
        scenarios = [e for e in entries if e.type == "scenario"]
    except Exception:
        return ""  # 容错，不阻断

    if not scenarios:
        return ""
    lines = ["## 候选测试场景（从中选择本 story 需要验证的）", ""]
    for s in scenarios:
        lines.append(f"- `{s.id}` — {s.title}")
        if getattr(s, "participating_services", None):
            lines.append(f"  服务: {', '.join(s.participating_services)}")
        if getattr(s, "apis", None):
            lines.append(f"  API: {', '.join(s.apis[:5])}")
        if getattr(s, "test_ref", ""):
            lines.append(f"  journey: {s.test_ref}")
    return "\n".join(lines)
```

### 2.2 注入规划 prompt

**文件**：`orchestrator/engine/planner.py`

在 `_build_agent_system_prompt`（`:154-236`）的 action_catalog 后（`:188`）注入：

```python
# 在 action_catalog = get_action_catalog_for_prompt() 之后追加：
scenario_catalog = build_scenario_catalog_section(story_key, workspace, "planning")
```

system prompt 模板里，在 `{action_catalog}` 和 `## 规则` 之间插入 `{scenario_catalog}`。

### 2.3 PlanResult 加 selected_scenarios 字段

**文件**：`orchestrator/engine/planner.py:322-329`（**注意：是内联的 live `StagePlan`，不是 `infra/schemas.py:20` 那个废弃的 PlanResult**）

```python
class StagePlan(BaseModel):
    stage: str
    skip: bool = False
    focus: str = ""
    task_actions: list[str] = []
    grill: bool = False
    selected_scenarios: list[str] = []   # ← 新增：scenario id 列表
```

输出格式 JSON（`planner.py:232` 附近）加：`"selected_scenarios": ["scenario:borrow-flow", ...]`

在 action 持久化处（`planner.py:429-479`），把 `selected_scenarios` 带进 `ctx["_agent_actions"]`，供 verify provider 读取（接线见 1.3 的 `_run_external_verify`——它把 `_agent_actions` 合入 done_data，修订点 R8）。

---

## 改动 3：board 加「测试场景」tab

### 3.1 最小改法（复用 DocsTab，约 5 行）

**文件**：`frontend/src/pages/StoryDetailPage.tsx`

`:13-19` MODULES 数组加一项。**修订点 R7**：tab 图标必须遵守 `frontend/AGENTS.md` 的 SVG 图标规则——下面 `icon` 字段仅为语义示意，实现时用现有 SVG 图标组件（参照其他 tab 的图标实现方式），不直接用 emoji 字符：

```tsx
const MODULES = [
  { id: 'overview', icon: IconOverview, label: '概览' },
  { id: 'code', icon: IconCode, label: '代码变更' },
  { id: 'docs', icon: IconDocs, label: '文档' },
  { id: 'scenarios', icon: IconFlask, label: '测试场景' },  // ← 新增（SVG 图标）
]
```

`:308-326` render 处加（复用 DocsTab，doc_type 开放，scenario_report 自动可用）：

```tsx
{validTab === 'scenarios' && <DocsTab storyKey={storyKey} />}
```

### 3.2 进阶（可选）：ScenariosTab 专用组件

如果想只显示 scenario 相关 doc_type + 最近 gate_result 状态，新建 `frontend/src/components/ScenariosTab.tsx`（约 50 行），调 `docApi.list` 过滤 + 显示 gate_result。非必须，DocsTab 已够用。

---

## 改动 4：scenario 知识闭环

### 4.1 ScenarioEntry 加字段

**文件**：`packages/knowledge/src/knowledge/models.py:75-99`

```python
@dataclass
class ScenarioEntry(KnowledgeEntry):
    # ... 现有字段 ...
    # 新增：跟可执行测试的绑定
    test_ref: str = ""          # 指向 hc-pytest journey YAML 路径
    last_run_at: str = ""       # 最近一次 journey 执行时间
    last_status: str = ""       # PASS / FAIL / STALE
    verified_at: str = ""       # 人工确认时间
```

同步更新 `to_dict()`。`parser.py:parse_scenario`（`:113-137`）从 frontmatter/sidecar JSON 读这些新字段。

### 4.2 填 apis 字段（代码静态分析）—— 修订点 R5：扫描器放 hc-pytest 仓

**新建**：`hc-pytest/scripts/generate_scenarios.py`（**在 hc-pytest 仓库，不在 story-miner**）

**归属理由（R5）**：扫 hc-* 服务的 `@RequestMapping`/`@PostMapping`/`@GetMapping` 注解是 HappyCash 专用逻辑，放进通用的 story-miner 包违反"不吸收"原则（方向相反但同一条红线）。**sidecar 机制本身通用、留在 knowledge 包；扫描器作为 hc 侧资产，story-lifecycle/miner 只消费产出的 sidecar JSON。**

核心逻辑（不变）：
- 扫 hc-* 服务的 `@RequestMapping`/`@PostMapping`/`@GetMapping` 注解 → 抽 API 路径
- 按 scenario 的 `participating_services` + `source_refs`（已有）归集 API
- 写 sidecar JSON：`<scenario>.md.json`（`{"apis": [...], "test_ref": "...", "updated_at": "..."}`）
- parser.py 已支持 playbook 同款 sidecar 机制，零改动即可索引

### 4.3 补 stale 检测 —— 修订点 R5b：比对 git 语义，不用文件 mtime

**文件**：`packages/story-lifecycle/src/story_lifecycle/knowledge/knowledge_store/stale.py:28`（`check_stale`）

从"只比 git commit hash"升级。**不用 `stat().st_mtime`**——`git checkout`、分支切换、编辑器 touch 都会刷 mtime，误报率极高；改用 `git log -1 --format=%ct -- <path>` 拿文件的真实最后变更时间：

```python
def check_stale(workspace):
    # ... 现有的 git commit 比对保留 ...

    # 新增：scenario 级 stale 检测
    scenarios_stale = []
    for entry in load_index_entries(workspace):
        if entry.type != "scenario":
            continue
        reasons = []
        # 比对 source_refs 代码文件的最后 git 变更时间 vs scenario verified_at
        for ref in entry.source_refs:
            code_file = Path(workspace) / ref
            if code_file.exists():
                # git 语义时间,不是 mtime(checkout/touch 会刷 mtime 造成误报)
                code_ts = git_last_change_ts(workspace, ref)  # git log -1 --format=%ct -- ref
                verified = parse_time(entry.verified_at) or 0
                if code_ts and code_ts > verified:
                    reasons.append(f"代码变更: {ref}")
        # test_ref 的 journey 最近 gate_result FAIL → stale
        if entry.last_status == "FAIL":
            reasons.append("绑定的 journey 最近失败")
        if reasons:
            scenarios_stale.append({"id": entry.id, "reasons": reasons})

    if scenarios_stale:
        return {"stale": True, "reason": f"{len(scenarios_stale)} 个 scenario 过期",
                "scenarios": scenarios_stale, "commit": current_commit}
    return {"stale": False, ...}
```

### 4.4 hc-pytest 结果回写（hc-pytest 侧，见改动 5）

hc-pytest 跑完 journey 后调：
- `story tool declare scenario_report <path>`（land 版本化 doc；路径指向 `<workspace>/story/`，使 `check_artifacts_landed` 可见——异步产物模式的关键接线，修订点 R1）
- `POST /api/story/{key}/gate-results`（`gate_name="scenario_tests"`，`evidence` 带每 journey pass/fail）

---

## 改动 5：hc-pytest 侧实现（参考，不在本仓库）

hc-pytest 需实现 `HcPytestVerifyProvider`（满足改动 1 的契约）：

```python
# hc-pytest/integrations/story_lifecycle_provider.py
# 修订点 R6：duck-type 实现——不 import story_lifecycle 的 ABC，
# 只保证有 verify() 方法；hc-pytest 运行环境因此无需安装 story-lifecycle。
class HcPytestVerifyProvider:
    def __init__(self, config: dict):
        self.config = config

    def verify(self, story_key, workspace, stage, done_data):
        # 1. 从 done_data["_agent_actions"] 取 selected_scenarios（R8 接线已合入）
        # 2. 默认异步模式（R1）：
        #    a. subprocess 起跑 journey（python -m pytest test_flows.py -k <scenario>）
        #       ——只 subprocess 调 CLI，不 import hc-pytest 业务代码进 serve 进程
        #       （hc-pytest 的依赖链 pytest/MQ client 等不需要进 story-lifecycle venv）
        #    b. 立即返回 None（本轮降级 LLM gate）
        #    c. 后台跑完：declare scenario_report 到 <workspace>/story/ +
        #       POST gate-results + 回写 scenario last_run_at/last_status（sidecar）
        # 3. config sync: true 时（小冒烟集）：同步跑、聚合 → VerifyResult，
        #    受 timeout_seconds 约束
        ...
```

**契约要点**：
- 同步模式：`verify()` 返回 `VerifyResult(passed=bool, findings=[...], evidence={...})`
- 异步模式：起跑后返回 `None`，结果经 scenario_report 产物 + gate-results 回写进入下一轮 gate
- 失败不抛异常（print + return None），保证不阻断 story 流程
- duck-type：有 `verify()` 方法即可，不强制继承 ABC（R6）
- 跨进程边界：journey 执行走 subprocess CLI，provider 入口模块本身必须依赖极轻（R6）

---

## 验收标准

改动完成后逐项验证：

- [ ] **不配 verify_provider 时零影响**：行为跟今天完全一致（LLM-only gate）
- [ ] **异步产物路径**：config 配了 provider 后，journey 起跑不阻塞 poll loop；journey 失败经 scenario_report 落地 + 下一轮 gate 能让 verify 转 retry，**且计入 reject budget**（连续失败会 force-escalate 而非死循环）
- [ ] **同步冒烟路径**：`sync: true` 时秒级测试集本轮即合并结果，受 timeout 约束
- [ ] **confirm-gate**：外部 PASS 不跳过人工 confirm（advance 仍需人确认）
- [ ] **single-pass**：单阶段 profile 下 scenario_report 同样能落地并进入 confirm 视野
- [ ] **规划注入**：`POST /api/story/{key}/plan` 的 LLM 输出含 `selected_scenarios`，且 `_agent_actions` 持久化后能在 gate 的 done_data 里读到（R8 接线）
- [ ] **board tab**：Story 详情页有「测试场景」tab（SVG 图标），能看到 scenario_report doc
- [ ] **stale 检测**：`story project sync-knowledge` 能列出过期 scenario（含具体差异原因；`git checkout`/touch 文件不造成误报）
- [ ] **apis 填充**：跑 hc-pytest 侧 `generate_scenarios.py` 后，至少一个 scenario 的 `apis` 非空
- [ ] **ScenarioEntry 新字段**：INDEX.json 里 scenario 条目有 `test_ref`/`last_run_at`/`last_status`/`verified_at`

---

## 不变量（执行时不能破坏）

1. **story-lifecycle 核心保持通用开源**——不硬依赖 HappyCash 特定代码（hc_order/RISK_RESULT/hc 服务注解扫描等；后者放 hc-pytest 仓，R5）
2. **AI 不自动覆盖正式 knowledge**——遵循 doc 07 原则：draft → lint → review → 人工确认 → merge
3. **新缝遵循先例模式**——ABC/duck-type 契约 + config importlib 加载 + 失败不阻断 + 默认 None
4. **两个 PlanResult 的坑**——改 `planner.py:322` 内联的 live `StagePlan`，**不要**改 `infra/schemas.py:20` 那个废弃的
5. **confirm-gate 不可跳过**（R3）——外部测试 PASS 只合并 findings，advance 永远经过人工 confirm（同 boundary_judge 的 confirm=true 不变量）
6. **外部 FAIL 计 reject budget**（R2）——防"环境坏→永远 retry"死循环
7. **不在 poll loop 里同步跑长测试**（R1）——同步模式仅冒烟集且受 timeout 约束

---

## 修订记录（2026-08-03）

初稿后经设计评审讨论，修订如下（与 `11-workspace-entity-design.md` 的结论对齐）：

| # | 修订点 | 原方案 | 新方案 | 理由 |
|---|---|---|---|---|
| R1 | 执行模式 | gate 内同步调 `provider.verify()` | **异步产物为主**（起跑即返回 None，journey 跑完 declare `scenario_report` 到 `<workspace>/story/`，下轮 gate 读证据）；同步仅冒烟集 + timeout | 同步调用阻塞 planner poll loop，分钟级 journey 会触发 stuck 误报；artifact-driven 是本仓库已确立的哲学 |
| R2 | 外部 FAIL 与重试 | 未定（"retry 或 fail 看枚举"） | 强制 `retry` 且**计入 reject budget**（≤3 + 理由去重，否则 force-escalate） | 环境挂/journey 坏时代码怎么改都过不了，不计预算必死循环 |
| R3 | 外部 PASS 与 confirm | 未提 | PASS 只合并 findings，**不跳过人工 confirm-gate** | 同 boundary_judge 的 confirm=true 不变量；LLM/测试通过都不能 auto-advance |
| R4 | single-pass 挂接 | 未提（默认多阶段 verify stage） | provider 挂 single-pass 唯一阶段的完成判定点，语义不变 | handoff/包干场景没有 verify stage，不接就永远不走外部验证 |
| R5 | 扫描器归属 | `generate_scenarios.py` 放 story-miner | 放 **hc-pytest 仓**；通用侧只消费 sidecar JSON | hc 注解扫描是专用逻辑，进通用 miner 违反"不吸收"红线 |
| R5b | stale 检测依据 | `stat().st_mtime` 比对 | `git log -1 --format=%ct -- <path>` 比对 | checkout/touch 刷 mtime，误报率高 |
| R6 | 跨仓依赖 | hc-pytest `import story_lifecycle...ABC` + sys.path 注入整个 hc-pytest | **duck-type**（加载器只查 `verify()` 方法）+ provider 只 **subprocess 调 CLI** | hc 侧不硬装 story-lifecycle；serve 进程不需要 hc-pytest 依赖链（pytest/MQ client） |
| R7 | tab 图标 | emoji `🧪` | SVG 图标组件 | `frontend/AGENTS.md` 的 SVG 图标规则 |
| R8 | selected_scenarios 接线 | "done_data 含 _agent_actions"（未说怎么含） | gate 调用方显式把 `ctx["_agent_actions"]` 合入 done_data（代码见 1.3） | selected_scenarios 在 context 不在 done.json，不接线 provider 永远拿不到 |

---

## 关联文档

- `11-workspace-entity-design.md` — Workspace 实体化设计（本设计的上层演进：异步产物模式的 evidence 归集、stale 检测升级为 probe 复跑、旅程展示为 WorkspacePage 投影）
- `07-scenario-knowledge-workflow-design.md` — Scenario Knowledge Layer 总设计（`scenario scan` 命令设计但未实现，本文档的改动 4 是其落地）
- `INTEGRATION.md` — miner × lifecycle 的 transcript 闭环（playbook/failure 有闭环，scenario 没有，本文档补上）
- hc-pytest 侧的完整架构决策存档：`D:/hc-all/hc-pytest/docs/plans/2026-08-03-golden-data-generator-and-story-lifecycle-integration.md`
