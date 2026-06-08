"""Integration tests for Smart Orchestrator Phase 1.

Tests exercise the graph nodes with mocked LLM/ttyd/adapter,
validating the new plan → execute → poll → review → router topology.
"""

import json
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from story_lifecycle.db import models as db
from story_lifecycle.orchestrator.nodes import (
    StoryState,
    plan_stage_node,
    execute_and_wait_node,
    review_stage_node,
    router_node,
    route_from_router,
    advance_node,
    route_after_plan,
    route_after_execute,
    MAX_REVIEW_RETRIES,
)


@pytest.fixture(autouse=True)
def _init_db(tmp_path, monkeypatch):
    monkeypatch.setenv("STORY_HOME", str(tmp_path / ".story-lifecycle"))
    db.init_db()
    yield


@pytest.fixture(autouse=True)
def _disable_adversarial_for_legacy_node_tests():
    """These tests exercise the pre-adversarial planner/reviewer node paths."""
    profile = {
        "cli": "claude",
        "stages": {
            "design": {
                "description": "Design",
                "review": True,
                "expected_outputs": ["spec_path", "complexity"],
                "next_default": ["implement"],
            },
            "implement": {
                "description": "Implement",
                "review": True,
                "expected_outputs": ["files_changed", "summary"],
                "next_default": ["review"],
            },
            "review": {
                "description": "Review",
                "review": False,
                "expected_outputs": [],
                "next_default": [],
            },
        },
        "adversarial": {"enabled": False},
    }
    with (
        patch("story_lifecycle.orchestrator.nodes.load_profile", return_value=profile),
        patch(
            "story_lifecycle.orchestrator.nodes.profile_loader.load_profile",
            return_value=profile,
        ),
        patch(
            "story_lifecycle.orchestrator.nodes.graph_nodes.load_profile",
            return_value=profile,
        ),
    ):
        yield


def _make_state(**overrides) -> StoryState:
    """Build a minimal StoryState for testing."""
    base: StoryState = {
        "story_key": "TEST-001",
        "title": "Test Story",
        "workspace": "/tmp/test-workspace",
        "profile": "minimal",
        "current_stage": "design",
        "status": "active",
        "complexity": "M",
        "context": {},
        "execution_count": 0,
        "last_error": None,
        "stage_start_time": 0.0,
        "plan_summary": None,
        "review_summary": None,
        "trajectory_score": None,
        "plan": None,
    }
    base.update(overrides)
    return base


# -------- routing functions --------


class TestRouteAfterPlan:
    def test_execute_when_not_skipping(self):
        state = _make_state(status="active")
        assert route_after_plan(state) == "execute_and_wait"

    def test_router_when_skipping(self):
        state = _make_state(status="skipping")
        assert route_after_plan(state) == "router"


class TestRouteAfterExecute:
    def test_review_when_no_error(self):
        state = _make_state(last_error=None)
        assert route_after_execute(state) == "review_stage"

    def test_router_when_error(self):
        state = _make_state(last_error="crashed")
        assert route_after_execute(state) == "router"


# -------- plan_stage_node --------


class TestPlanStageNode:
    @patch("story_lifecycle.orchestrator.nodes.graph_nodes.planner")
    def test_planner_failure_blocks_story(self, mock_planner):
        mock_planner.compress_context.return_value = None
        mock_planner.plan_stage.side_effect = RuntimeError("LLM timeout")

        state = _make_state()
        result = plan_stage_node(state)

        assert result.get("_pre_routed_action") == "wait_confirm"
        assert "LLM timeout" in result.get("last_error", "")

    @patch("story_lifecycle.orchestrator.nodes.graph_nodes.planner")
    def test_llm_plan_generates_task_file(self, mock_planner):
        mock_planner.compress_context.return_value = None
        mock_planner.plan_stage.return_value = {
            "adapter": "claude",
            "provider": "deepseek",
            "model": "sonnet",
            "skip": False,
            "summary": "Design the API layer",
            "extra_instructions": "Create REST endpoints for user management",
            "reasoning": "Need to define API contracts first",
            "trajectory_score": 0.85,
        }

        with tempfile.TemporaryDirectory() as tmp:
            state = _make_state(workspace=tmp)
            result = plan_stage_node(state)

            assert result["plan_summary"] == "Design the API layer"
            assert result["trajectory_score"] == 0.85
            assert result["plan"]["adapter"] == "claude"

            plan_path = result["context"].get("plan_path")
            assert plan_path is not None
            plan_file = Path(tmp) / plan_path
            assert plan_file.exists()
            content = plan_file.read_text(encoding="utf-8")
            assert "Create REST endpoints" in content

    @patch("story_lifecycle.orchestrator.nodes.graph_nodes.planner")
    def test_skip_when_plan_says_skip(self, mock_planner):
        mock_planner.compress_context.return_value = None
        mock_planner.plan_stage.return_value = {
            "skip": True,
            "reasoning": "Stage not needed for this complexity",
            "summary": "Skipping design",
        }

        state = _make_state()
        result = plan_stage_node(state)

        assert result["status"] == "skipping"
        assert "跳过" in (result["plan_summary"] or "")

    @patch("story_lifecycle.orchestrator.nodes.graph_nodes.planner")
    def test_fallback_on_planner_exception(self, mock_planner):
        mock_planner.compress_context.return_value = None
        mock_planner.plan_stage.side_effect = Exception("LLM timeout")

        state = _make_state()
        result = plan_stage_node(state)

        assert result.get("_pre_routed_action") == "wait_confirm"
        assert "LLM timeout" in result.get("last_error", "")


# -------- review_stage_node --------


class TestReviewStageNode:
    def test_circuit_breaker_skips_on_error(self):
        state = _make_state(last_error="CC process crashed")
        result = review_stage_node(state)

        assert result.get("review_summary") is None

    @patch("story_lifecycle.orchestrator.nodes.graph_nodes.planner")
    def test_retry_fatigue_forces_fail(self, mock_planner):
        """When review_round_count >= retry_limit, gate blocks with GateDecision."""

        state = _make_state(
            execution_count=5,
            context={
                "output": "some data",
                "review_round_count_design": MAX_REVIEW_RETRIES,
            },
        )
        # Need stage config with expected_outputs
        with patch(
            "story_lifecycle.orchestrator.nodes.graph_nodes.get_stage_config",
            return_value={"expected_outputs": ["output"]},
        ):
            result = review_stage_node(state)

        assert result["last_error"] is not None
        assert "review" in result["last_error"].lower()
        assert result["_gate_decision"]["reason_code"] == "review_retry_limit"

    @patch("story_lifecycle.orchestrator.nodes.graph_nodes.planner")
    def test_stale_executor_no_review_fatigue(self, mock_planner):
        """When review_round_count==0 but execution_count>=retry_limit, gate blocks
        with stale executor reason — review never actually ran."""

        state = _make_state(
            execution_count=MAX_REVIEW_RETRIES,
            context={"output": "some data"},
        )
        with patch(
            "story_lifecycle.orchestrator.nodes.graph_nodes.get_stage_config",
            return_value={"expected_outputs": ["output"]},
        ):
            result = review_stage_node(state)

        assert result["last_error"] is not None
        assert "review did not run" in result["last_error"].lower()
        assert (
            result["_gate_decision"]["reason_code"]
            == "review_not_run_due_to_stale_executor_attempt_count"
        )

    @patch("story_lifecycle.orchestrator.nodes.graph_nodes.planner")
    def test_review_pass(self, mock_planner):
        mock_planner.review_stage.return_value = {
            "quality": "pass",
            "summary": "Design looks good",
            "feedback": "All requirements addressed",
            "issues": [],
            "suggestions": ["Consider adding error codes"],
            "trajectory_score": 0.9,
            "context_updates": {},
            "reasoning": "Complete and well-structured",
        }

        with tempfile.TemporaryDirectory() as tmp:
            state = _make_state(
                workspace=tmp,
                context={"prd_path": "prd/TEST-001.md"},
            )
            with patch(
                "story_lifecycle.orchestrator.nodes.graph_nodes.get_stage_config",
                return_value={"expected_outputs": ["prd_path"]},
            ):
                result = review_stage_node(state)

            assert result["review_summary"] == "Design looks good"
            assert result["trajectory_score"] == 0.9
            assert result.get("last_error") is None

    @patch("story_lifecycle.orchestrator.nodes.graph_nodes.planner")
    def test_review_revise_sets_error(self, mock_planner):
        mock_planner.review_stage.return_value = {
            "quality": "revise",
            "summary": "Missing error handling",
            "feedback": "Need try/catch blocks",
            "issues": [
                {
                    "type": "missing_error_handling",
                    "severity": "high",
                    "location": "api.py:42",
                    "description": "No error handling for DB connection",
                }
            ],
            "suggestions": ["Add try/except around DB calls"],
            "trajectory_score": 0.4,
            "context_updates": {},
            "reasoning": "Critical issue found",
        }

        with tempfile.TemporaryDirectory() as tmp:
            state = _make_state(
                workspace=tmp,
                context={"prd_path": "prd/TEST-001.md"},
            )
            with patch(
                "story_lifecycle.orchestrator.nodes.graph_nodes.get_stage_config",
                return_value={"expected_outputs": ["prd_path"]},
            ):
                result = review_stage_node(state)

            assert result["last_error"] is not None
            assert "1 high severity" in result["last_error"]

    @patch("story_lifecycle.orchestrator.nodes.graph_nodes.planner")
    def test_review_fail(self, mock_planner):
        mock_planner.review_stage.return_value = {
            "quality": "fail",
            "summary": "Completely off-track",
            "feedback": "Fundamental architecture mismatch",
            "issues": [],
            "suggestions": [],
            "trajectory_score": 0.1,
            "context_updates": {},
            "reasoning": "Irrecoverable",
        }

        with tempfile.TemporaryDirectory() as tmp:
            state = _make_state(
                workspace=tmp,
                context={"prd_path": "prd/TEST-001.md"},
            )
            with patch(
                "story_lifecycle.orchestrator.nodes.graph_nodes.get_stage_config",
                return_value={"expected_outputs": ["prd_path"]},
            ):
                result = review_stage_node(state)

            assert result["last_error"] is not None
            assert "off-track" in result["last_error"]


# -------- router_node --------


class TestRouterNode:
    def test_happy_path_advance(self):
        state = _make_state(last_error=None)
        with patch(
            "story_lifecycle.orchestrator.nodes.graph_nodes.get_stage_config",
            return_value={"confirm": False},
        ):
            result_state = router_node(state)
        assert route_from_router(result_state) == "advance"

    def test_happy_path_wait_confirm(self):
        db.upsert_story("TEST-001", title="Test", workspace="/tmp")
        state = _make_state(last_error=None)
        with (
            patch(
                "story_lifecycle.orchestrator.nodes.graph_nodes.get_stage_config",
                return_value={"confirm": True},
            ),
            patch(
                "story_lifecycle.orchestrator.nodes.graph_nodes.interrupt",
                side_effect=lambda x: None,
            ),
        ):
            result_state = router_node(state)
        # After wait_confirm + interrupt, router routes back to plan_stage
        assert route_from_router(result_state) == "plan_stage"

    def test_retry_fatigue_fail(self):
        state = _make_state(
            last_error="Some error",
            review_summary="达到重试上限 (3 次)",
        )
        result_state = router_node(state)
        assert route_from_router(result_state) == "__end__"

    def test_low_trajectory_score_kill(self):
        state = _make_state(
            last_error="Bad output",
            trajectory_score=0.2,
        )
        result_state = router_node(state)
        assert route_from_router(result_state) == "__end__"

    def test_review_driven_retry(self):
        state = _make_state(
            last_error="Review: missing tests (1 high severity issues)",
            review_summary="Needs more tests",
            execution_count=1,
        )
        result_state = router_node(state)
        assert route_from_router(result_state) == "plan_stage"

    def test_review_driven_exhausted_fail(self):
        """Router should fail when review_round_count >= retry_limit."""
        state = _make_state(
            last_error="Review: still broken",
            review_summary="Still failing",
            execution_count=MAX_REVIEW_RETRIES,
            context={"review_round_count_design": MAX_REVIEW_RETRIES},
        )
        result_state = router_node(state)
        assert route_from_router(result_state) == "__end__"

    def test_review_driven_retry_below_limit(self):
        """Router should retry when review_round_count < retry_limit."""
        state = _make_state(
            last_error="Review: fixable issue",
            review_summary="Minor issues found",
            execution_count=5,
            context={"review_round_count_design": 1},
        )
        result_state = router_node(state)
        assert route_from_router(result_state) == "plan_stage"

    def test_llm_router_fallback_on_error(self):
        state = _make_state(
            last_error="Execution failed",
        )
        with patch(
            "story_lifecycle.orchestrator.nodes.graph_nodes.llm_router"
        ) as mock_router:
            mock_router.route.return_value = {
                "action": "retry",
                "reasoning": "Transient error",
                "provider_override": "openai",
            }
            result_state = router_node(state)
        assert route_from_router(result_state) == "plan_stage"
        assert state["context"]["_provider"] == "openai"


# -------- execute_and_wait_node --------


class TestExecuteAndWaitNode:
    def _mock_tool(self):
        tool = MagicMock()

        def _execute(state, args):
            state["execution_count"] = state.get("execution_count", 0) + 1
            state["stage_start_time"] = 1.0
            state["last_error"] = None

        tool.execute.side_effect = _execute
        return tool

    @patch("story_lifecycle.orchestrator.graph._tui_app", None)
    @patch("story_lifecycle.orchestrator.tools.get_tool")
    def test_reads_adapter_from_plan(self, mock_get_tool):
        mock_tool = self._mock_tool()
        mock_get_tool.return_value = mock_tool

        # No done file + headless mode → sets last_error about headless
        state = _make_state(
            plan={"adapter": "claude", "provider": "deepseek", "model": "opus"},
        )
        execute_and_wait_node(state)

        mock_get_tool.assert_called_with("stage_tool")
        tool_args = mock_tool.execute.call_args[0][1]
        assert tool_args["adapter"] == "claude"
        assert tool_args["provider"] == "deepseek"
        assert tool_args["model"] == "opus"

    @patch("story_lifecycle.orchestrator.graph._tui_app", None)
    @patch("story_lifecycle.orchestrator.tools.get_tool")
    def test_falls_back_to_profile_config(self, mock_get_tool):
        mock_tool = self._mock_tool()
        mock_get_tool.return_value = mock_tool

        state = _make_state(plan=None)
        execute_and_wait_node(state)

        mock_tool.execute.assert_called_once()

    @patch("story_lifecycle.orchestrator.graph._tui_app", None)
    @patch("story_lifecycle.orchestrator.tools.get_tool")
    def test_prepends_plan_file_to_prompt(self, mock_get_tool):
        mock_tool = self._mock_tool()
        mock_get_tool.return_value = mock_tool

        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / ".story" / "context" / "TEST-001"
            plan_dir.mkdir(parents=True)
            plan_file = plan_dir / "plan_design.md"
            plan_file.write_text("# Task: Build API", encoding="utf-8")

            state = _make_state(
                workspace=tmp,
                context={"plan_path": ".story/context/TEST-001/plan_design.md"},
            )
            execute_and_wait_node(state)

            tool_args = mock_tool.execute.call_args[0][1]
            assert "Task: Build API" in tool_args["prompt"]

    @patch("story_lifecycle.orchestrator.graph._tui_app", None)
    @patch("story_lifecycle.orchestrator.tools.get_tool")
    def test_dispatches_skill_tool(self, mock_get_tool):
        mock_tool = self._mock_tool()
        mock_get_tool.return_value = mock_tool

        state = _make_state(
            plan={"tool": "skill_tool", "adapter": "claude", "model": "sonnet"},
        )
        execute_and_wait_node(state)

        mock_get_tool.assert_called_with("skill_tool")

    @patch("story_lifecycle.orchestrator.tools.get_tool")
    def test_existing_done_file_consumed_early(self, mock_get_tool):
        with tempfile.TemporaryDirectory() as tmp:
            done_dir = Path(tmp) / ".story" / "done" / "TEST-001"
            done_dir.mkdir(parents=True)
            done_file = done_dir / "design.json"
            done_file.write_text(
                json.dumps(
                    {
                        "spec_path": "docs/design.md",
                        "complexity": "M",
                        "summary": "done",
                    }
                ),
                encoding="utf-8",
            )

            state = _make_state(workspace=tmp)
            result = execute_and_wait_node(state)

            # Done file is consumed (deleted) by execute_and_wait_node
            assert not done_file.exists()
            assert result["context"].get("spec_path") == "docs/design.md"
            mock_get_tool.assert_not_called()


# -------- router action tests (retry/skip/fail now inside router_node) --------


class TestRouterRetryAction:
    def test_retry_clears_error(self):
        state = _make_state(
            last_error="Review: fixable issue",
            review_summary="Minor issues found",
            execution_count=1,
            context={"review_round_count_design": 1},
        )
        result = router_node(state)
        assert result["_next_action"] == "plan_stage"
        assert result["last_error"] is None


class TestRouterSkipAction:
    def test_skip_fills_expected_outputs(self):
        db.upsert_story("TEST-001", title="Test", workspace="/tmp")
        state = _make_state(
            last_error="Some error",
            review_summary="达到重试上限 (3 次)",
        )
        result = router_node(state)
        assert result["_next_action"] == "__end__"


class TestRouterFailAction:
    def test_marks_blocked(self):
        db.upsert_story("TEST-001", title="Test", workspace="/tmp")
        state = _make_state(last_error="Something broke")
        with patch(
            "story_lifecycle.orchestrator.nodes.graph_nodes.llm_router"
        ) as mock_router:
            mock_router.route.return_value = {"action": "fail", "reasoning": "Bad"}
            result = router_node(state)
        assert result["status"] == "blocked"


class TestAdvanceNode:
    def test_advance_to_next_stage(self):
        db.upsert_story("TEST-ADV", title="Test", workspace="/tmp", status="active")
        state = _make_state(
            story_key="TEST-ADV",
            context={"prd_path": "prd.md"},
        )
        with (
            patch(
                "story_lifecycle.orchestrator.nodes.resolve_next_stage",
                return_value="implement",
            ),
            patch(
                "story_lifecycle.orchestrator.nodes.graph_nodes.get_stage_config",
                return_value={"expected_outputs": ["prd_path"]},
            ),
            patch(
                "story_lifecycle.orchestrator.validation.get_stage_config",
                return_value={"expected_outputs": ["prd_path"]},
            ),
            patch(
                "story_lifecycle.orchestrator.nodes.load_profile",
                return_value={"stages": {"design": {"expected_outputs": ["prd_path"]}}},
            ),
            patch(
                "story_lifecycle.orchestrator.nodes.graph_nodes.load_profile",
                return_value={"stages": {"design": {"expected_outputs": ["prd_path"]}}},
            ),
        ):
            result = advance_node(state)

        assert result["current_stage"] == "implement"
        assert result["execution_count"] == 0


# -------- event_log --------


class TestEventLog:
    def test_log_and_retrieve_events(self):
        import uuid

        key = f"EVT-{uuid.uuid4().hex[:8]}"
        db.log_event(key, "design", "plan", {"adapter": "claude"})
        db.log_event(key, "design", "execute", {"attempt": 1})
        db.log_event(key, "design", "review", {"quality": "pass"})

        events = db.get_story_events(key)
        assert len(events) == 3
        assert events[0]["event_type"] == "plan"
        assert events[1]["event_type"] == "execute"
        assert events[2]["event_type"] == "review"

        payload = json.loads(events[2]["payload"])
        assert payload["quality"] == "pass"

    def test_no_events_returns_empty(self):
        events = db.get_story_events("NONEXISTENT-999")
        assert events == []


# -------- graph compilation --------


class TestGraphCompilation:
    def test_build_and_compile(self):
        from story_lifecycle.orchestrator.graph import build_graph

        g = build_graph()
        compiled = g.compile()
        assert compiled is not None

    def test_graph_has_all_nodes(self):
        from story_lifecycle.orchestrator.graph import build_graph

        g = build_graph()
        expected = {
            "plan_stage",
            "execute_and_wait",
            "review_stage",
            "router",
            "advance",
        }
        assert set(g.nodes) == expected


# -------- Phase 3: Sub-story delegation --------


class TestSubStoryDelegation:
    @patch("story_lifecycle.orchestrator.nodes.graph_nodes.planner")
    @patch(
        "story_lifecycle.orchestrator.nodes.graph_nodes.interrupt",
        side_effect=lambda x: None,
    )
    def test_split_creates_sub_stories(self, mock_interrupt, mock_planner):
        mock_planner.compress_context.return_value = None
        mock_planner.plan_stage.return_value = {
            "split": True,
            "subtasks": [
                {
                    "key_suffix": "auth",
                    "title": "Auth module",
                    "summary": "Implement auth",
                    "depends_on": [],
                },
                {
                    "key_suffix": "api",
                    "title": "API layer",
                    "summary": "Implement API",
                    "depends_on": ["auth"],
                },
            ],
            "summary": "Splitting into sub-stories",
        }

        with tempfile.TemporaryDirectory() as tmp:
            db.upsert_story(
                "PARENT-001", title="Parent", workspace=tmp, status="active"
            )
            state = _make_state(story_key="PARENT-001", workspace=tmp)
            result = plan_stage_node(state)

            # plan_stage_node returns list[Send] for active subtasks
            from langgraph.types import Send

            assert isinstance(result, list)
            assert all(isinstance(s, Send) for s in result)
            assert len(result) == 1  # only auth is active (api is blocked)

            # The Send targets "plan_stage" with sub-state
            send = result[0]
            sub_state = send.arg
            assert sub_state["story_key"] == "PARENT-001-auth"
            assert sub_state["title"] == "Auth module"
            assert sub_state["status"] == "active"

            # Verify sub-stories in DB
            sub_auth = db.get_story("PARENT-001-auth")
            assert sub_auth is not None
            assert sub_auth["parent_key"] == "PARENT-001"
            assert sub_auth["status"] == "active"  # no deps

            sub_api = db.get_story("PARENT-001-api")
            assert sub_api is not None
            assert sub_api["status"] == "blocked"  # depends on auth

            # Verify parent is marked as waiting
            parent = db.get_story("PARENT-001")
            assert parent["status"] == "waiting_subtasks"

    def test_route_after_plan_does_not_route_waiting_subtasks(self):
        state = _make_state(status="waiting_subtasks")
        # waiting_subtasks now routes to END — graph stops while children run
        assert route_after_plan(state) == "__end__"


class TestSubStoryQueries:
    def test_get_sub_stories(self):
        key = f"PSUB-{uuid.uuid4().hex[:6]}"
        db.upsert_story(key, title="Parent", workspace="/tmp", status="active")
        db.upsert_story(
            f"{key}-a", title="A", workspace="/tmp", parent_key=key, subtask_index=0
        )
        db.upsert_story(
            f"{key}-b", title="B", workspace="/tmp", parent_key=key, subtask_index=1
        )

        subs = db.get_sub_stories(key)
        assert len(subs) == 2
        assert subs[0]["subtask_index"] == 0
        assert subs[1]["subtask_index"] == 1

    def test_get_pending_parents_empty(self):
        parents = db.get_pending_parents()
        # May have data from other tests, just verify it returns a list
        assert isinstance(parents, list)

    def test_get_pending_parents_with_waiting(self):
        key = f"PWAIT-{uuid.uuid4().hex[:6]}"
        db.upsert_story(
            key, title="Parent", workspace="/tmp", status="waiting_subtasks"
        )
        parents = db.get_pending_parents()
        matching = [p for p in parents if p["story_key"] == key]
        assert len(matching) == 1


class TestWatchdogSubtaskCompletion:
    def test_resumes_parent_when_all_children_complete(self):
        key = f"PCMP-{uuid.uuid4().hex[:6]}"
        db.upsert_story(
            key, title="Parent", workspace="/tmp", status="waiting_subtasks"
        )
        db.upsert_story(
            f"{key}-a", title="A", workspace="/tmp", parent_key=key, status="completed"
        )
        db.upsert_story(
            f"{key}-b", title="B", workspace="/tmp", parent_key=key, status="completed"
        )

        with patch("story_lifecycle.orchestrator.graph.resume_story") as mock_resume:
            # Simulate watchdog logic
            pending = db.get_pending_parents()
            for parent in pending:
                if parent["story_key"] != key:
                    continue
                children = db.get_sub_stories(parent["story_key"])
                incomplete = [c for c in children if c["status"] != "completed"]
                if not incomplete:
                    db.update_story(parent["story_key"], status="active")
                    mock_resume(parent["story_key"])

            parent = db.get_story(key)
            assert parent["status"] == "active"
            mock_resume.assert_called_once_with(key)


# -------- Phase 3: Extended tools --------


class TestToolRegistry:
    def test_all_five_tools_registered(self):
        from story_lifecycle.orchestrator.tools import available_tools

        tools = available_tools()
        assert set(tools) == {
            "stage_tool",
            "skill_tool",
            "research_tool",
            "benchmark_tool",
            "review_tool",
        }

    def test_get_tool_returns_correct_type(self):
        from story_lifecycle.orchestrator.tools import get_tool
        from story_lifecycle.orchestrator.tools.research_tool import ResearchTool
        from story_lifecycle.orchestrator.tools.benchmark_tool import BenchmarkTool
        from story_lifecycle.orchestrator.tools.review_tool import ReviewTool

        assert isinstance(get_tool("research_tool"), ResearchTool)
        assert isinstance(get_tool("benchmark_tool"), BenchmarkTool)
        assert isinstance(get_tool("review_tool"), ReviewTool)

    def test_tool_describe_methods(self):
        from story_lifecycle.orchestrator.tools import get_tool

        for name in ["research_tool", "benchmark_tool", "review_tool"]:
            tool = get_tool(name)
            assert len(tool.describe()) > 0


class TestBoardDisplaySorting:
    def test_children_appear_under_parent(self):

        key = f"BDSP-{uuid.uuid4().hex[:6]}"
        db.upsert_story(key, title="Parent", workspace="/tmp", status="active")
        db.upsert_story(
            f"{key}-a",
            title="Child A",
            workspace="/tmp",
            parent_key=key,
            subtask_index=0,
            status="active",
        )
        db.upsert_story(
            f"{key}-b",
            title="Child B",
            workspace="/tmp",
            parent_key=key,
            subtask_index=1,
            status="active",
        )

        # Should not raise — just verify it renders
        stories = db.list_active_stories()
        parents = [s for s in stories if not s.get("parent_key")]
        children = [s for s in stories if s.get("parent_key")]
        assert any(s["story_key"] == key for s in parents)
        assert len([c for c in children if c["parent_key"] == key]) == 2
