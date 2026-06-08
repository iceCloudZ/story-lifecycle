"""Unified error handling for graph nodes."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class NodeError:
    """Structured node error — use _set_error() to apply to state.

    Not raised as exception (LangGraph expects nodes to return state dicts).
    Instead, passed to _set_error() which sets last_error + logs in one call.
    """

    node: str
    stage: str
    message: str
    error_type: str = ""
    recoverable: bool = True
    action: str = "set_last_error"
    meta: dict = field(default_factory=dict)

    def apply(self, state: dict) -> dict:
        """Set last_error and log node error. Returns state for chaining."""
        state["last_error"] = self.message

        from ..observability import log_node_error

        log_node_error(
            state.get("story_key", ""),
            self.stage,
            self.node,
            self.error_type or type(self).__name__,
            self.message[:200],
            execution_count=state.get("execution_count", 0),
            recoverable=self.recoverable,
            action=self.action,
            **self.meta,
        )
        return state
