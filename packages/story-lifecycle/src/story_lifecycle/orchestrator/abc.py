"""全局编排线程抽象层（设计 13）— StageExecutor / DecisionHandler / PromptBuilder。

编排线程（scheduler.py）只通过这三个接口操作 story，不关心具体是半自动
（手动 spawn，编排线程只 poll+judge）还是全自动（自动 spawn + poll + judge）。
子类在 executors.py / handlers.py / prompts.py。

设计 13 契约：
- StageExecutor: stage 的 spawn PTY / poll artifacts / 判断完成
- DecisionHandler: judge 决策（approve/reject/escalate）的副作用执行
- PromptBuilder: stage prompt 构建（统一 planner._build_cli_prompt 与 api 两处）
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class StageExecutor(ABC):
    """stage 执行器抽象——定义 spawn PTY / poll artifacts / 判断完成的契约。

    编排线程通过这个接口操作 stage，不关心具体是半自动(手动 spawn)还是全自动(自动 spawn)。
    子类：
    - InteractiveStageExecutor: 半自动模式（等人点「启动 CLI」，编排线程只 poll+judge）
    - AutomaticStageExecutor: 全自动模式（编排线程自动 spawn + poll + judge）
    """

    def maybe_spawn(self, story_key: str, stage: str, ctx: dict) -> None:
        """没有 PTY 且 stage 需要执行时被编排线程调用。

        Default: 不自动 spawn（半自动行为，等人点「启动 CLI」）。
        AutomaticStageExecutor 覆写为自动 spawn。
        """

    @abstractmethod
    def get_pty(self, story_key: str, stage: str):
        """获取当前 stage 的 PTY（可能 None=未 spawn）。"""

    @abstractmethod
    def spawn(self, story_key: str, stage: str, action: dict) -> str:
        """spawn PTY for stage，返回 session_id。"""

    @abstractmethod
    def is_artifacts_ready(self, story_key: str, stage: str) -> bool:
        """stage 的成果物是否全部落地。"""


class DecisionHandler(ABC):
    """judge 决策处理抽象——定义 approve/reject/escalate 三分支的契约。

    编排线程调 judge 拿到决策后，通过这个接口执行副作用。
    子类：
    - InteractiveDecisionHandler: 半自动（reject→pause等人，approve→推进）
    - AutomaticDecisionHandler: 全自动（reject→插retry重做，approve→推进+spawn next）
    """

    @abstractmethod
    def handle_approve(
        self,
        story_key: str,
        stage: str,
        decision: dict,
        ctx: dict,
        actions: list,
    ) -> bool:
        """approve: 写 _completed_stages + lifecycle 推进 + summary。返回是否 paused_for_confirm。"""

    @abstractmethod
    def handle_reject(
        self,
        story_key: str,
        stage: str,
        decision: dict,
        ctx: dict,
        actions: list,
    ) -> None:
        """reject: 半自动→pause；全自动→插 retry action。"""

    @abstractmethod
    def handle_escalate(self, story_key: str, stage: str, decision: dict, ctx: dict) -> None:
        """escalate: pause 等人。"""


class PromptBuilder(ABC):
    """stage prompt 构建抽象——统一 _build_cli_prompt / _build_stage_launch_prompt /
    _build_interactive_stage_prompt 三处重复的 prompt 构建。

    子类按 stage 类型（design/build/verify）或 profile 类型提供具体实现。
    """

    @abstractmethod
    def build(
        self,
        story_key: str,
        stage: str,
        workspace: str,
        ctx: dict,
        action: dict,
    ) -> str:
        """构建 stage 的 CLI prompt。"""
