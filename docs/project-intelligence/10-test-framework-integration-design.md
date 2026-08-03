# Test Framework Integration Design

> 把外部专用测试框架（hc-pytest）接入 story-lifecycle 的 verify 门禁 + scenario 知识闭环。
> 本文档自包含——执行者无需访问 hc-pytest 仓库即可完成全部改动。

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
        """执行外部测试，返回结果。返回 None 表示不参与（降级到 LLM gate）。

        参数：
          story_key: story 标识
          workspace: 工作区路径
          stage: 当前阶段（通常 "verify"）
          done_data: 含 _agent_actions（规划时 LLM 选的 selected_scenarios 等）
        """
        ...
```

### 1.2 新建 `orchestrator/verify_providers/__init__.py`

```python
# packages/story-lifecycle/src/story_lifecycle/orchestrator/verify_providers/__init__.py
"""config 驱动的 verify provider 加载（mirror context_providers）。"""
from __future__ import annotations
import importlib
from pathlib import Path
from typing import Optional
from .base import BaseVerifyProvider


def load_verify_provider(config: dict) -> Optional[BaseVerifyProvider]:
    """从 config 加载 verify provider。未配置返回 None。"""
    cfg = config.get("verify_provider")
    if not cfg:
        return None
    try:
        # 可选 sys.path prepend（加载非已安装包，如 hc-pytest）
        if cfg.get("path"):
            import sys
            p = cfg["path"]
            if p not in sys.path:
                sys.path.insert(0, p)
        module = importlib.import_module(cfg["module"])
        cls = getattr(module, cfg["class"])
        return cls(config=cfg)
    except Exception as e:
        # 容错：加载失败不阻断，降级到 LLM-only gate
        print(f"[verify_provider] 加载失败，降级到 LLM gate: {e}")
        return None
```

### 1.3 接入 verify gate

**文件**：`orchestrator/evaluation/unified_gate.py`（`run_unified_verify_gate` 函数，约 `:61`）

在 LLM 判定完成后，追加调一次外部 verify provider，合并结果：

```python
# 在 run_unified_verify_gate 的 LLM 判定逻辑之后追加：

def _run_external_verify(story_key, workspace, done_data) -> Optional[VerifyResult]:
    """如果配了 verify_provider，执行外部测试。"""
    from orchestrator.verify_providers import load_verify_provider
    from infra.config import load_config  # 或现有的 config 读取方式
    config = load_config()
    provider = load_verify_provider(config)
    if provider is None:
        return None
    try:
        return provider.verify(story_key, workspace, "verify", done_data)
    except Exception as e:
        print(f"[external verify] 执行失败，忽略: {e}")
        return None

# 在 gate decision 汇总处：
ext = _run_external_verify(story_key, workspace, done_data)
if ext:
    if not ext.passed:
        # 外部测试失败 → 阻断 advance（或转 retry）
        decision = "retry"  # 或 "fail"，看现有 gate 的 decision 枚举
        repair_action = f"外部测试失败: {ext.summary}"
    # 把 findings 合并进 quality findings
    for f in ext.findings:
        record_finding(story_key, category="test_failure", **f)
```

### 1.4 config 格式（HappyCash 部署的 `~/.story-lifecycle/config.yaml`）

```yaml
# 默认不配 = LLM-only gate（开源用户零影响）
# HappyCash 部署才配：
verify_provider:
  module: "hc_pytest.integrations.story_lifecycle_provider"
  class: "HcPytestVerifyProvider"
  path: "D:/hc-all/hc-pytest"           # hc-pytest 仓库路径（加到 sys.path）
  journeys_dir: "D:/hc-all/hc-pytest/journeys"
```

---

## 改动 2：规划注入 scenario_catalog

让 planner LLM 在规划时看到候选 scenario，输出 `selected_scenarios`。

### 2.1 新增 `build_scenario_catalog_section`

**文件**：`orchestrator/engine/prompt_sections.py`（在现有 `build_knowledge_section` 旁，约 `:177`）

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

在 `_build_agent_system_prompt`（约 `:154-236`）的 action_catalog 后（约 `:188`）注入：

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

在 action 持久化处（`planner.py:429-479`），把 `selected_scenarios` 带进 `ctx["_agent_actions"]`，供 verify provider 读取。

---

## 改动 3：board 加「测试场景」tab

### 3.1 最小改法（复用 DocsTab，约 5 行）

**文件**：`frontend/src/pages/StoryDetailPage.tsx`

`:13-19` MODULES 数组加一项：

```tsx
const MODULES = [
  { id: 'overview', icon: '📊', label: '概览' },
  { id: 'code', icon: '📦', label: '代码变更' },
  { id: 'docs', icon: '📄', label: '文档' },
  { id: 'scenarios', icon: '🧪', label: '测试场景' },  // ← 新增
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

同步更新 `to_dict()`。`parser.py:parse_scenario`（约 `:113-137`）从 frontmatter/sidecar JSON 读这些新字段。

### 4.2 填 apis 字段（代码静态分析）

**新建**：`packages/story-miner/scripts/generate_scenarios.py`（mirror `generate_playbooks.py` 的结构）

核心逻辑：
- 扫 hc-* 服务的 `@RequestMapping`/`@PostMapping`/`@GetMapping` 注解 → 抽 API 路径
- 按 scenario 的 `participating_services` + `source_refs`（已有）归集 API
- 写 sidecar JSON：`<scenario>.md.json`（`{"apis": [...], "test_ref": "...", "updated_at": "..."}`）
- parser.py 已支持 playbook 同款 sidecar 机制，零改动即可索引

接入 `refresh.sh` 加一步：
```bash
echo "[scenario] generate scenario index ..."
"$PYTHON" scripts/generate_scenarios.py || echo "  (scenario gen failed, skip)"
```

### 4.3 补 stale 检测

**文件**：`packages/story-lifecycle/src/story_lifecycle/knowledge/knowledge_store/stale.py:28`（`check_stale`）

从"只比 git commit hash"升级：

```python
def check_stale(workspace):
    # ... 现有的 git commit 比对保留 ...

    # 新增：scenario 级 stale 检测
    scenarios_stale = []
    for entry in load_index_entries(workspace):
        if entry.type != "scenario":
            continue
        reasons = []
        # 比对 source_refs 代码文件 mtime vs scenario verified_at
        for ref in entry.source_refs:
            code_file = Path(workspace) / ref
            if code_file.exists():
                code_mtime = code_file.stat().st_mtime
                verified = parse_time(entry.verified_at) or 0
                if code_mtime > verified:
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
- `story tool declare scenario_report <path>`（land 版本化 doc）
- `POST /api/story/{key}/gate-results`（`gate_name="scenario_tests"`，`evidence` 带每 journey pass/fail）

---

## 改动 5：hc-pytest 侧实现（参考，不在本仓库）

hc-pytest 需实现 `HcPytestVerifyProvider`（满足改动 1 的 ABC 契约）：

```python
# hc-pytest/integrations/story_lifecycle_provider.py
from story_lifecycle.orchestrator.verify_providers.base import BaseVerifyProvider, VerifyResult

class HcPytestVerifyProvider(BaseVerifyProvider):
    def verify(self, story_key, workspace, stage, done_data):
        # 1. 从 done_data["_agent_actions"] 取 selected_scenarios
        # 2. 跑对应 journey（subprocess: python -m pytest test_flows.py -k <scenario>）
        # 3. 聚合 pass/fail → VerifyResult
        # 4. 回写 scenario 的 last_run_at/last_status（写 .story/knowledge sidecar）
        ...
```

**契约要点**：
- `verify()` 返回 `VerifyResult(passed=bool, findings=[...], evidence={...})`
- 返回 `None` = 不参与（降级 LLM gate）
- 失败不抛异常（print + return None），保证不阻断 story 流程

---

## 验收标准

改动完成后逐项验证：

- [ ] **不配 verify_provider 时零影响**：行为跟今天完全一致（LLM-only gate）
- [ ] **config 配了 verify_provider**：hc-pytest journey 失败能让 verify gate 阻断/转 retry
- [ ] **规划注入**：`POST /api/story/{key}/plan` 的 LLM 输出含 `selected_scenarios`
- [ ] **board tab**：Story 详情页有「测试场景」tab，能看到 scenario_report doc
- [ ] **stale 检测**：`story project sync-knowledge` 能列出过期 scenario（含具体差异原因）
- [ ] **apis 填充**：跑 `generate_scenarios.py` 后，至少一个 scenario 的 `apis` 非空
- [ ] **ScenarioEntry 新字段**：INDEX.json 里 scenario 条目有 `test_ref`/`last_run_at`/`last_status`/`verified_at`

---

## 不变量（执行时不能破坏）

1. **story-lifecycle 核心保持通用开源**——不硬依赖 HappyCash 特定代码（hc_order/RISK_RESULT 等）
2. **AI 不自动覆盖正式 knowledge**——遵循 doc 07 原则：draft → lint → review → 人工确认 → merge
3. **新缝遵循三先例模式**——ABC + config importlib 加载 + 失败不阻断 + 默认 None
4. **两个 PlanResult 的坑**——改 `planner.py:322` 内联的 live `StagePlan`，**不要**改 `infra/schemas.py:20` 那个废弃的

---

## 关联文档

- `07-scenario-knowledge-workflow-design.md` — Scenario Knowledge Layer 总设计（`scenario scan` 命令设计但未实现，本文档的改动 4 是其落地）
- `INTEGRATION.md` — miner × lifecycle 的 transcript 闭环（playbook/failure 有闭环，scenario 没有，本文档补上）
- hc-pytest 侧的完整架构决策存档：`D:/hc-all/hc-pytest/docs/plans/2026-08-03-golden-data-generator-and-story-lifecycle-integration.md`
