"""
Story 路由决策链 — Router / Policy / Gate 三者权责的显式文档
============================================================

Java 类比：DecisionManager — 原来 router_node 中 170 行的 if-else 链
在这里被展开为命名的、有优先级的决策步骤。

三个决策者的权责边界
--------------------

  Router   → 决定"下一步动作是什么"（advance / retry / skip / fail / wait_confirm）
           → 8 级优先级链，确定性规则 + LLM 回退
           → 调用者：router_node（nodes.py:1392）

  Gate     → 决定"审查是否通过"（pass / revise / fail / wait_confirm）
           → 检查 review_round_count、执行次数、门禁条件
           → 调用者：review_stage_node、对抗循环（evaluator_loop）

  Policy   → 决定"操作是否允许"（allow / reject / needs_confirm / shadow_only）
           → 目前只在 Copilot 建议操作中使用，不在主图流程中
           → 调用者：TUI _execute_copilot_action（tui.py:2472）

三者协作关系
----------

  review_stage_node
    ├── 跑完审查 → 产出 GateDecision（pass/revise/fail）
    ├── 设 last_error + review_summary（如 revise/fail）
    ├── 设 _pre_routed_action="wait_confirm"（如达到上限）
    └──→ router_node

  router_node
    ├── 检查 _pre_routed_action（Gate 预设的覆盖）
    ├── 检查 review_summary（Gate 的审查结论）
    ├── 按 8 级决策链判定 → _next_action
    └──→ advance / retry / skip / fail / wait_confirm

  TUI _execute_copilot_action
    ├── 用户通过 Copilot 请求操作
    ├── Policy Engine 裁决：forbidden / apply / confirm
    └──→ 执行或拒绝

注意：Policy Engine 目前不在主图流程中使用。
主图通过 _pre_routed_action + last_error + review_summary 传递 Gate 结论给 Router。
"""

from dataclasses import dataclass

# ============================================================
# 决策链定义
# ============================================================


@dataclass
class DecisionStep:
    """一级决策步骤 — 带优先级和人类可读的文档。"""

    priority: int  # 1-8，越小越优先
    name: str  # 人类可读的名称
    description: str  # 触发条件
    produces: str  # 产生的 action
    rule_source: str = ""  # 规则来源（router_node 中的行号）


# Router 的 8 级决策优先级 — 显式定义为数据
DECISION_CHAIN: list[DecisionStep] = [
    DecisionStep(
        priority=1,
        name="预路由覆盖",
        description="对抗循环或 planner 通过 _pre_routed_action 预设了动作",
        produces="直接使用预设值（通常是 wait_confirm 或 advance）",
        rule_source="nodes.py:1402-1406",
    ),
    DecisionStep(
        priority=2,
        name="重试疲劳",
        description="review_summary 包含 '达到重试上限'",
        produces="fail",
        rule_source="nodes.py:1409-1417",
    ),
    DecisionStep(
        priority=3,
        name="低轨迹评分",
        description="trajectory_score 不为 None 且 < 0.3（Planner 评分）",
        produces="fail",
        rule_source="nodes.py:1420-1434",
    ),
    DecisionStep(
        priority=4,
        name="快乐路径",
        description="last_error 为空，当前阶段无错误",
        produces="advance（如 stage 配置了 confirm 则为 wait_confirm）",
        rule_source="nodes.py:1437-1444",
    ),
    DecisionStep(
        priority=5,
        name="缺少预期输出",
        description='last_error 以 "Missing expected outputs:" 开头',
        produces="fail",
        rule_source="nodes.py:1447-1458",
    ),
    DecisionStep(
        priority=6,
        name="审查驱动重试",
        description="last_error 和 review_summary 同时存在，review_round_count < retry_limit",
        produces="retry（超过 max_rounds 则为 fail）",
        rule_source="nodes.py:1460-1503",
    ),
    DecisionStep(
        priority=7,
        name="执行次数上限",
        description="execution_count >= stage max_retries（默认 3）",
        produces="wait_confirm（需人工介入）",
        rule_source="nodes.py:1506-1525",
    ),
    DecisionStep(
        priority=8,
        name="LLM 回退",
        description="以上 7 条确定性规则均不匹配",
        produces="调用 LLM Router API 决定 retry / skip / fail",
        rule_source="nodes.py:1528-1558",
    ),
]


# ============================================================
# Router / Policy / Gate 权责文档
# ============================================================

ROUTER_DOC = """
Router（路由器）— orchestrator/router.py
  输入：StoryState + stage_config
  输出：{"action": "retry|skip|fail", "reasoning": "...", "provider_override": "..."}
  模式：调用 OpenAI 兼容 API（DeepSeek 默认），LLM 路由只在规则链的最后一步被调用
  配置：STORY_LLM_API_KEY / STORY_LLM_BASE_URL / STORY_LLM_MODEL 环境变量
"""

GATE_DOC = """
Gate（门禁）— orchestrator/gate.py
  核心类型：GateDecision（dataclass，gate.py:48-128）
  决策值：advance / retry_stage / retry_review / wait_confirm / fail / accept_risk_advance
  检查项：
    1. review_round_count >= retry_limit → wait_confirm（审查轮次疲劳）
    2. review_round_count == 0 且 execution_count >= retry_limit → wait_confirm（陈旧执行器）
  集成点：
    - review_stage_node（nodes.py:588-670）：在运行审查循环之前检查门禁条件
    - evaluator_loop（evaluator_loop.py）：在对抗循环中调用 gate
    - wait_confirm_node（nodes.py:1763）：持久化 GateDecision 到 DB
    - TUI：展示 gate 状态，提供 resume / accept_risk_advance 操作
"""

POLICY_DOC = """
Policy Engine（策略引擎）— orchestrator/policy_engine.py
  核心类型：DecisionEnvelope + PolicyDecision + AutonomyLevel（Enum）
  自主等级：SHADOW（仅记录）/ CONFIRM（需确认）/ APPLY（自动执行）/ FORBIDDEN（禁止）
  默认规则表（policy_engine.py:22-27）：
    - read_only      → APPLY
    - local_config   → CONFIRM
    - workflow_state → CONFIRM
    - destructive    → FORBIDDEN
  3 次连续拒绝后 ANY action → FORBIDDEN
  集成点：
    - TUI _execute_copilot_action（tui.py:2472）：仅用于 Copilot 建议操作的执行门控
    - 不在主图流程中（router_node 不调用 Policy Engine）
"""


# ============================================================
# 辅助函数
# ============================================================


def describe_decision_chain() -> str:
    """返回人类可读的决策链描述（用于文档和调试）。"""
    lines = ["## Router 决策链（8 级优先级）", ""]
    for step in sorted(DECISION_CHAIN, key=lambda s: s.priority):
        lines.append(f"### {step.priority}. {step.name}")
        lines.append(f"- **触发条件**: {step.description}")
        lines.append(f"- **产出**: {step.produces}")
        lines.append(f"- **来源**: `{step.rule_source}`")
        lines.append("")
    return "\n".join(lines)


def describe_modules() -> str:
    """返回 Router / Policy / Gate 三者权责的人类可读描述。"""
    return "\n".join(
        [
            "## 决策模块权责划分",
            "",
            ROUTER_DOC.strip(),
            "",
            GATE_DOC.strip(),
            "",
            POLICY_DOC.strip(),
        ]
    )
