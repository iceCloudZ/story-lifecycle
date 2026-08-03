"""外部测试框架的验证契约。

story-lifecycle 是通用引擎，不硬依赖任何特定测试框架。
具体实现（如 hc-pytest 的 HcPytestVerifyProvider）通过 config 注入。
失败返回 None 不阻断 verify（同 BaseStoryContextProvider 的容错哲学）。

对应设计：docs/project-intelligence/10-test-framework-integration-design.md 改动 1。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class VerifyResult:
    """外部测试框架的验证结果。"""

    passed: bool = False
    summary: str = ""  # 一句话总结
    findings: list[dict] = field(
        default_factory=list
    )  # [{scenario, status, detail}]
    evidence: dict = field(default_factory=dict)  # 任意结构化证据（journey pass/fail 明细）
    evidence_ref: str = ""  # 报告路径等


class BaseVerifyProvider(ABC):
    """外部测试框架注入 verify gate 的契约。

    config 加载方式 mirror context_providers/__init__.py:_load_provider。
    默认 None（不配 verify_provider）→ 今天的 LLM-only gate，零行为变化。
    """

    def __init__(self, config: dict):
        self.config = config

    @abstractmethod
    def verify(
        self,
        story_key: str,
        workspace: str,
        stage: str,
        done_data: dict,
    ) -> VerifyResult | None:
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
        ...  # pragma: no cover — ABC 契约
