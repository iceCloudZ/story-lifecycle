"""Stage Graph — allowed stage transitions and graph topology.

The Stage Graph defines which stage transitions are legal. Unlike
a fixed linear profile, the Stage Graph allows branching, looping,
and conditional transitions, enabling runtime flexibility while
maintaining safety constraints.

Design doc: idea-orchestrator-agent.md §Stage Graph
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..stage_library import get_stage_definition


@dataclass
class StageEdge:
    """A directed edge between two stages in the graph.

    Attributes:
        from_stage: Source stage name.
        to_stage: Target stage name.
        condition: Optional condition description for this transition.
        risk_delta: Additional risk incurred by this transition.
        allowed_triggers: Who/what can trigger this transition.
        metadata: Additional edge properties.
    """

    from_stage: str
    to_stage: str
    condition: str = ""
    risk_delta: float = 0.0
    allowed_triggers: list[str] = field(default_factory=lambda: ["router", "human"])
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class StageGraph:
    """A directed graph of allowed stage transitions.

    The graph defines the universe of legal paths through the
    orchestration workflow. Runtime deviations (Graph Patches)
    must be validated against this graph.
    """

    name: str
    edges: list[StageEdge] = field(default_factory=list)
    entry_stage: str = "plan"
    exit_stages: list[str] = field(
        default_factory=lambda: ["test", "final_review", "deploy"]
    )

    def get_successors(self, stage: str) -> list[str]:
        """Get all stages that can follow the given stage.

        Args:
            stage: Current stage name.

        Returns:
            List of valid successor stage names.
        """
        return [e.to_stage for e in self.edges if e.from_stage == stage]

    def get_predecessors(self, stage: str) -> list[str]:
        """Get all stages that can precede the given stage.

        Args:
            stage: Current stage name.

        Returns:
            List of valid predecessor stage names.
        """
        return [e.from_stage for e in self.edges if e.to_stage == stage]

    def is_valid_transition(self, from_stage: str, to_stage: str) -> bool:
        """Check if a transition between two stages is legal.

        Args:
            from_stage: Source stage.
            to_stage: Target stage.

        Returns:
            True if the transition is allowed in this graph.
        """
        return any(
            e.from_stage == from_stage and e.to_stage == to_stage for e in self.edges
        )

    def validate_path(self, path: list[str]) -> list[str]:
        """Validate a complete path through the graph.

        Args:
            path: List of stage names forming a path.

        Returns:
            List of validation errors (empty if path is valid).
        """
        errors: list[str] = []

        if not path:
            errors.append("path is empty")
            return errors

        if path[0] != self.entry_stage:
            errors.append(f"path must start with '{self.entry_stage}', got '{path[0]}'")

        for i in range(len(path) - 1):
            if not self.is_valid_transition(path[i], path[i + 1]):
                errors.append(f"invalid transition: {path[i]} → {path[i + 1]}")

        if path[-1] not in self.exit_stages:
            errors.append(
                f"path must end at one of {self.exit_stages}, got '{path[-1]}'"
            )

        return errors

    def find_all_paths(
        self, from_stage: str, to_stage: str, max_depth: int = 10
    ) -> list[list[str]]:
        """Find all valid paths between two stages (DFS with cycle detection).

        Args:
            from_stage: Start stage.
            to_stage: End stage.
            max_depth: Maximum path length to search.

        Returns:
            List of valid paths (each path is a list of stage names).
        """
        results: list[list[str]] = []

        def _dfs(current: str, path: list[str], visited: set[str]) -> None:
            if len(path) > max_depth:
                return
            if current == to_stage:
                results.append(list(path))
                return
            for successor in self.get_successors(current):
                if successor not in visited:
                    visited.add(successor)
                    path.append(successor)
                    _dfs(successor, path, visited)
                    path.pop()
                    visited.remove(successor)

        _dfs(from_stage, [from_stage], {from_stage})
        return results


# ── default graph ──


def build_default_graph() -> StageGraph:
    """Build the default stage graph with standard transitions.

    This graph allows the common orchestration patterns:
    - Linear: plan → implement → test
    - With plan review: plan → plan_review → implement → test
    - With code review: plan → implement → review → test
    - With architecture review: implement → architecture_review → test
    - Human intervention: any → human_review → same stage (retry)
    - Loops: review → plan (revise loop), test → implement (fix loop)
    """
    edges = [
        # Standard flow
        StageEdge("plan", "implement", condition="plan accepted"),
        StageEdge("plan", "plan_review", condition="strict mode"),
        StageEdge("plan_review", "implement", condition="plan review pass"),
        StageEdge("plan_review", "plan", condition="plan review revise"),
        StageEdge("implement", "review", condition="standard mode"),
        StageEdge("implement", "test", condition="simple mode"),
        StageEdge("review", "test", condition="review pass"),
        StageEdge("review", "implement", condition="review revise"),
        StageEdge("test", "final_review", condition="test pass"),
        StageEdge("test", "implement", condition="test fail, fix needed"),
        # Architecture review (high-risk path)
        StageEdge("implement", "architecture_review", condition="high risk detected"),
        StageEdge("architecture_review", "test", condition="arch review pass"),
        StageEdge("architecture_review", "implement", condition="arch review revise"),
        StageEdge(
            "architecture_review", "human_review", condition="arch review uncertain"
        ),
        # Final review
        StageEdge("final_review", "deploy", condition="final review pass"),
        StageEdge("final_review", "implement", condition="final review revise"),
        # Deploy
        StageEdge("deploy", "final_review", condition="deploy fail"),
        # Human review (can enter from any stage)
        StageEdge("plan", "human_review", condition="planner uncertain"),
        StageEdge("implement", "human_review", condition="execution blocked"),
        StageEdge("review", "human_review", condition="review uncertain"),
        StageEdge("test", "human_review", condition="test ambiguous"),
        StageEdge("human_review", "plan", condition="human requests replan"),
        StageEdge("human_review", "implement", condition="human requests retry"),
        StageEdge("human_review", "test", condition="human accepts risk"),
    ]

    return StageGraph(
        name="default",
        edges=edges,
        entry_stage="plan",
        exit_stages=["test", "final_review", "deploy"],
    )


def build_simple_graph() -> StageGraph:
    """Build a simple graph for trivial stories (S scope).

    Linear: plan → implement → test
    """
    edges = [
        StageEdge("plan", "implement"),
        StageEdge("implement", "test"),
    ]
    return StageGraph(
        name="simple",
        edges=edges,
        entry_stage="plan",
        exit_stages=["test"],
    )


def build_strict_graph() -> StageGraph:
    """Build a strict graph with all review gates.

    plan → plan_review → implement → review → architecture_review → test → final_review
    """
    edges = [
        StageEdge("plan", "plan_review"),
        StageEdge("plan_review", "implement", condition="pass"),
        StageEdge("plan_review", "plan", condition="revise"),
        StageEdge("implement", "review"),
        StageEdge("review", "architecture_review", condition="pass"),
        StageEdge("review", "implement", condition="revise"),
        StageEdge("architecture_review", "test", condition="pass"),
        StageEdge("architecture_review", "implement", condition="revise"),
        StageEdge("test", "final_review", condition="pass"),
        StageEdge("test", "implement", condition="fail"),
        StageEdge("final_review", "deploy", condition="pass"),
        StageEdge("final_review", "implement", condition="revise"),
    ]
    return StageGraph(
        name="strict",
        edges=edges,
        entry_stage="plan",
        exit_stages=["deploy"],
    )


# ── graph validation ──


def validate_graph(graph: StageGraph) -> list[str]:
    """Validate a StageGraph for structural issues.

    Checks:
    - Entry stage exists in library
    - Exit stages exist in library
    - All edge endpoints reference valid stages
    - No isolated stages (except entry/exit)
    - No self-loops

    Args:
        graph: The graph to validate.

    Returns:
        List of validation errors (empty if valid).
    """
    errors: list[str] = []

    # Check entry stage
    if not get_stage_definition(graph.entry_stage):
        errors.append(f"entry stage '{graph.entry_stage}' not in stage library")

    # Check exit stages
    for es in graph.exit_stages:
        if not get_stage_definition(es):
            errors.append(f"exit stage '{es}' not in stage library")

    # Check edges
    referenced_stages: set[str] = set()
    for edge in graph.edges:
        referenced_stages.add(edge.from_stage)
        referenced_stages.add(edge.to_stage)

        if edge.from_stage == edge.to_stage:
            errors.append(f"self-loop detected: {edge.from_stage} → {edge.to_stage}")

        # Note: We allow references to stages not in BUILTIN_STAGES
        # (custom stages may be defined by profiles)

    # Check reachability from entry
    reachable: set[str] = set()
    frontier = {graph.entry_stage}
    while frontier:
        stage = frontier.pop()
        if stage in reachable:
            continue
        reachable.add(stage)
        for successor in graph.get_successors(stage):
            frontier.add(successor)

    unreachable = referenced_stages - reachable - {graph.entry_stage}
    if unreachable:
        errors.append(f"unreachable stages from entry: {unreachable}")

    return errors
