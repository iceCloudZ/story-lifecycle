# Headless E2E Test Tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement a headless E2E test framework that runs the full LangGraph lifecycle (design → implement → test → completed) in 1-3 seconds per scenario, without real AI/tmux/ttyd/network.

**Architecture:** Replace real tool execution with a `FakeStageTool` that writes `.story-done/{story_key}/{stage}.json` directly. Scenarios defined in YAML drive the fake tool's output per stage. An E2E runner helper patches planner/tools/ttyd, creates isolated DB, and asserts final state. The full graph runs through its real nodes — only execution is faked.

**Tech Stack:** Python 3.10+, pytest, PyYAML, LangGraph, SQLite

---

## File Structure

```
tests/
  conftest.py                          # Shared fixtures (isolated DB, graph globals reset)
  e2e/
    __init__.py
    conftest.py                        # E2E-specific fixtures (scenario loader, fake tool)
    scenario.py                        # Scenario class — loads YAML, provides stage payloads
    fake_tool.py                       # FakeStageTool — writes .story-done without real AI
    runner.py                          # run_scenario() helper — patches, runs graph, returns result
    test_headless_lifecycle.py         # Parametrized E2E test cases
    scenarios/
      happy_path.yaml
      markdown_done_json.yaml
      missing_expected_output.yaml
      review_retry_then_pass.yaml
      sub_story_wait_resume.yaml
pyproject.toml                         # Add testpaths config
```

---

### Task 1: Add `testpaths` to `pyproject.toml`

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add pytest testpaths config**

Add to end of `pyproject.toml`:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 2: Verify pytest collects only tests/**

Run: `python -m pytest --collect-only -q 2>&1 | head -5`
Expected: No files from `examples/calculator/tests` appear.

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml
git commit -m "chore: set pytest testpaths to avoid collecting examples"
```

---

### Task 2: Create `tests/conftest.py` with isolated DB fixture

**Files:**
- Create: `tests/conftest.py`

- [ ] **Step 1: Write the shared conftest**

```python
"""Shared pytest fixtures — isolated DB and graph globals reset."""

import threading
from pathlib import Path

import pytest

from story_lifecycle.db import models as db
from story_lifecycle.orchestrator import graph


@pytest.fixture(autouse=True)
def _reset_graph_globals():
    """Clear in-process graph state before and after every test."""
    graph._running_stories.clear()
    graph._workspace_locks.clear()
    graph._plan_done.clear()
    graph._terminal_opened.clear()
    with graph._running_lock:
        graph._running_stories.clear()
    yield
    graph._running_stories.clear()
    graph._workspace_locks.clear()
    graph._plan_done.clear()
    graph._terminal_opened.clear()


@pytest.fixture
def isolated_story_home(tmp_path, monkeypatch):
    """Provide an isolated ~/.story-lifecycle directory for testing."""
    story_home = tmp_path / "story-home"
    story_home.mkdir()
    db_path = story_home / "story.db"
    checkpoint_path = story_home / "checkpoint.db"

    monkeypatch.setattr(db.models, "get_db_path", lambda: db_path)
    monkeypatch.setattr(graph, "checkpoint_db", checkpoint_path)
    monkeypatch.setattr(db.models, "get_db_path", lambda: db_path)

    # Patch STORY_HOME in nodes too
    import story_lifecycle.orchestrator.nodes as nodes_mod
    monkeypatch.setattr(nodes_mod, "STORY_HOME", story_home)

    db.init_db()
    return story_home
```

- [ ] **Step 2: Verify existing tests still pass**

Run: `python -m pytest tests/ -x -q`
Expected: All existing tests pass. If any fail due to the new autouse fixture, investigate and fix.

- [ ] **Step 3: Commit**

```bash
git add tests/conftest.py
git commit -m "test: add shared conftest with isolated DB and graph globals reset"
```

---

### Task 3: Create `tests/e2e/scenario.py` — Scenario loader

**Files:**
- Create: `tests/e2e/__init__.py`
- Create: `tests/e2e/scenario.py`

- [ ] **Step 1: Write `tests/e2e/__init__.py`**

Empty file.

- [ ] **Step 2: Write `tests/e2e/scenario.py`**

```python
"""Scenario — loads a YAML test scenario and provides stage payloads."""

from pathlib import Path
from typing import Any

import yaml


class Scenario:
    """Represents a single E2E test scenario loaded from YAML."""

    def __init__(self, path: str | Path):
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        self.story_key: str = raw["story_key"]
        self.title: str = raw.get("title", "")
        self.profile: str = raw.get("profile", "minimal")
        self.stages: dict[str, dict] = raw.get("stages", {})
        self.reviews: dict[str, dict] = raw.get("reviews", {})
        self.expect: dict = raw.get("expect", {})

    def stage_payload(self, stage: str, execution_index: int = 1) -> dict[str, Any]:
        """Return the done-file payload for a given stage.

        execution_index is 1-based. If the stage uses `executions` array,
        pick the element at execution_index-1 (clamped to last element).
        Otherwise use the single `done` dict or `raw_done` string.
        """
        stage_cfg = self.stages.get(stage, {})

        # Multiple executions defined
        if "executions" in stage_cfg:
            execs = stage_cfg["executions"]
            idx = min(execution_index - 1, len(execs) - 1)
            return execs[idx].get("done", {})

        # Raw done (for testing invalid JSON)
        if "raw_done" in stage_cfg:
            return stage_cfg["raw_done"]

        # Single done payload
        return stage_cfg.get("done", {})

    def stage_raw_done(self, stage: str) -> str | None:
        """Return raw_done string if the stage defines one, else None."""
        stage_cfg = self.stages.get(stage, {})
        return stage_cfg.get("raw_done")

    def review_payload(self, stage: str, execution_index: int = 1) -> dict:
        """Return the review result for a given stage at a given execution.

        If no reviews are defined for the stage, returns {"quality": "pass"}.
        """
        stage_reviews = self.reviews.get(stage, {})
        if "executions" in stage_reviews:
            execs = stage_reviews["executions"]
            idx = min(execution_index - 1, len(execs) - 1)
            return execs[idx]
        return stage_reviews if stage_reviews else {"quality": "pass"}
```

- [ ] **Step 3: Write a quick smoke test to verify loading works**

This is a manual check — run:

```python
# From project root, in Python:
from tests.e2e.scenario import Scenario
# Will test once we have YAML files in Task 6
```

No standalone test needed — Task 7 will exercise this end-to-end.

- [ ] **Step 4: Commit**

```bash
git add tests/e2e/__init__.py tests/e2e/scenario.py
git commit -m "test(e2e): add Scenario loader for YAML-driven E2E tests"
```

---

### Task 4: Create `tests/e2e/fake_tool.py` — FakeStageTool

**Files:**
- Create: `tests/e2e/fake_tool.py`

- [ ] **Step 1: Write FakeStageTool**

```python
"""FakeStageTool — writes .story-done without real AI, for headless E2E."""

import json
from pathlib import Path

from story_lifecycle.db import models as db


class FakeStageTool:
    """Replaces real tool execution. Writes .story-done based on scenario config."""

    def __init__(self, scenario):
        self.scenario = scenario

    def execute(self, state: dict, args: dict) -> dict:
        key = state["story_key"]
        stage = state["current_stage"]
        workspace = state["workspace"]
        next_count = state.get("execution_count", 0) + 1

        done_dir = Path(workspace) / ".story-done" / key
        done_dir.mkdir(parents=True, exist_ok=True)
        done_file = done_dir / f"{stage}.json"

        # Check if scenario has raw_done (for testing invalid JSON)
        raw = self.scenario.stage_raw_done(stage)
        if raw is not None:
            done_file.write_text(str(raw), encoding="utf-8")
        else:
            payload = self.scenario.stage_payload(stage, execution_index=next_count)
            done_file.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )

        db.log_event(
            key,
            stage,
            "execute",
            {"attempt": next_count, "tool": "fake_stage_tool"},
        )

        return {
            **state,
            "execution_count": next_count,
            "stage_start_time": 0.0,
            "last_error": None,
        }

    def describe(self) -> str:
        return "FakeStageTool for headless E2E testing"
```

- [ ] **Step 2: Commit**

```bash
git add tests/e2e/fake_tool.py
git commit -m "test(e2e): add FakeStageTool for headless E2E tests"
```

---

### Task 5: Create `tests/e2e/runner.py` — E2E runner helper

**Files:**
- Create: `tests/e2e/runner.py`

- [ ] **Step 1: Write the runner**

```python
"""E2E runner — patches deps, runs graph, returns result for assertion."""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

from story_lifecycle.db import models as db
from story_lifecycle.orchestrator import graph as graph_mod
from story_lifecycle.orchestrator import planner
from story_lifecycle.orchestrator.nodes import StoryState

from .scenario import Scenario
from .fake_tool import FakeStageTool


class E2EResult:
    """Holds the result of a headless E2E run."""

    def __init__(self, story_key: str, workspace: str):
        self.story_key = story_key
        self.workspace = workspace
        self.story: dict | None = None
        self.events: list[dict] = []
        self.final_state: dict | None = None

    def refresh(self):
        self.story = db.get_story(self.story_key)
        self.events = db.get_story_events(self.story_key)


def run_scenario(scenario: Scenario, workspace: Path) -> E2EResult:
    """Run a full headless E2E lifecycle for a scenario.

    - Creates the story in DB
    - Patches planner, tools, ttyd, notify
    - Calls _run_story_impl() directly (synchronous)
    - Returns E2EResult for assertion
    """
    key = scenario.story_key

    # Create story in DB
    db.upsert_story(
        key,
        title=scenario.title,
        workspace=str(workspace),
        profile=scenario.profile,
        current_stage="design",
        status="active",
    )

    # Write PRD placeholder if scenario expects it in context
    # (some tests need prd_path to exist)
    prd_dir = workspace / "prd"
    prd_dir.mkdir(exist_ok=True)
    prd_file = prd_dir / f"{key}.md"
    prd_file.write_text(f"# {scenario.title}\n\nTest PRD content.\n", encoding="utf-8")

    fake_tool = FakeStageTool(scenario)

    # Build review mock that returns scenario-defined review results
    def _mock_review_stage(state, cfg, stage_output):
        stage = state["current_stage"]
        exec_count = state.get("execution_count", 1)
        return scenario.review_payload(stage, execution_index=exec_count)

    with (
        patch("story_lifecycle.orchestrator.nodes.planner") as mock_planner,
        patch("story_lifecycle.orchestrator.tools.get_tool") as mock_get_tool,
        patch("story_lifecycle.orchestrator.nodes.ttyd") as mock_ttyd,
        patch("story_lifecycle.orchestrator.nodes.notify"),
        patch("story_lifecycle.orchestrator.graph.emit_plan_done"),
        patch("story_lifecycle.orchestrator.graph.emit_terminal_opened"),
        patch("story_lifecycle.orchestrator.nodes.interrupt", side_effect=lambda x: None),
    ):
        # Disable real LLM planner
        mock_planner.is_available.return_value = False
        mock_planner.compress_context.return_value = None

        # If scenario has reviews, enable planner with mock review
        if scenario.reviews:
            mock_planner.is_available.return_value = True
            mock_planner.review_stage.side_effect = _mock_review_stage
            mock_planner.plan_stage.return_value = {
                "adapter": "claude",
                "provider": "deepseek",
                "model": "sonnet",
                "skip": False,
                "summary": "Fallback plan",
                "extra_instructions": "",
                "reasoning": "test",
                "trajectory_score": 0.8,
            }

        # Fake tool dispatch
        mock_get_tool.return_value = fake_tool

        # Fake ttyd — session always alive
        mock_ttyd.session_name.return_value = f"story-{key}"
        mock_ttyd.session_alive.return_value = True
        mock_ttyd._MPLEX = None  # Skip session crash detection

        # Run the graph synchronously
        graph_mod._run_story_impl(key)

    result = E2EResult(key, str(workspace))
    result.refresh()
    return result
```

- [ ] **Step 2: Commit**

```bash
git add tests/e2e/runner.py
git commit -m "test(e2e): add E2E runner helper with full patching"
```

---

### Task 6: Create scenario YAML files

**Files:**
- Create: `tests/e2e/scenarios/happy_path.yaml`
- Create: `tests/e2e/scenarios/markdown_done_json.yaml`
- Create: `tests/e2e/scenarios/missing_expected_output.yaml`
- Create: `tests/e2e/scenarios/review_retry_then_pass.yaml`
- Create: `tests/e2e/scenarios/sub_story_wait_resume.yaml`

- [ ] **Step 1: Write `happy_path.yaml`**

```yaml
story_key: E2E-HAPPY
title: Headless happy path
profile: minimal

stages:
  design:
    done:
      spec_path: docs/spec.md
      complexity: S
      summary: design completed
  implement:
    done:
      implementation_summary: implemented
  test:
    done:
      tests_passed: true

expect:
  status: completed
  final_stage: test
  context:
    spec_path: docs/spec.md
    complexity: "S"
```

- [ ] **Step 2: Write `markdown_done_json.yaml`**

```yaml
story_key: E2E-MDJSON
title: Markdown-wrapped JSON parsing
profile: minimal

stages:
  design:
    raw_done: |
      Here is the result:
      ```json
      {
        "spec_path": "docs/spec.md",
        "complexity": "S",
        "summary": "design in markdown fence"
      }
      ```
  implement:
    done:
      implementation_summary: implemented
  test:
    done:
      tests_passed: true

expect:
  status: completed
```

- [ ] **Step 3: Write `missing_expected_output.yaml`**

```yaml
story_key: E2E-MISS
title: Missing expected output field
profile: minimal

stages:
  design:
    done:
      complexity: S

expect:
  status: blocked
```

- [ ] **Step 4: Write `review_retry_then_pass.yaml`**

```yaml
story_key: E2E-RETRY
title: Review retry then pass
profile: minimal

stages:
  design:
    executions:
      - done:
          spec_path: docs/spec.md
          complexity: S
          summary: first draft with flaw
      - done:
          spec_path: docs/spec.md
          complexity: S
          summary: revised draft
  implement:
    done:
      implementation_summary: implemented
  test:
    done:
      tests_passed: true

reviews:
  design:
    executions:
      - quality: revise
        summary: missing edge cases
        issues:
          - type: missing_tests
            severity: high
            location: docs/spec.md
            description: Edge cases are not covered
        suggestions:
          - Add edge case coverage
        trajectory_score: 0.4
        context_updates: {}
        reasoning: need more tests
      - quality: pass
        summary: design accepted
        issues: []
        suggestions: []
        trajectory_score: 0.9
        context_updates: {}
        reasoning: looks good now

expect:
  status: completed
```

- [ ] **Step 5: Write `sub_story_wait_resume.yaml`**

```yaml
story_key: E2E-SUB
title: Sub-story wait and resume
profile: minimal

# This scenario tests the sub-story delegation flow.
# The planner mock will be overridden in the test itself
# since sub-story uses interrupt() which needs special handling.

stages:
  design:
    done:
      spec_path: docs/spec.md
      complexity: S
      summary: design done

expect:
  # Sub-story scenario is tested differently — see test body
  status: waiting_subtasks
```

- [ ] **Step 6: Commit**

```bash
git add tests/e2e/scenarios/
git commit -m "test(e2e): add 5 headless E2E scenario YAML files"
```

---

### Task 7: Create `tests/e2e/conftest.py` — E2E fixtures

**Files:**
- Create: `tests/e2e/conftest.py`

- [ ] **Step 1: Write E2E conftest**

```python
"""E2E test fixtures — scenario loading and workspace setup."""

from pathlib import Path

import pytest

from .scenario import Scenario

SCENARIOS_DIR = Path(__file__).parent / "scenarios"


@pytest.fixture
def scenario(request):
    """Load a scenario by name. Use indirect parametrization."""
    name = request.param
    return Scenario(SCENARIOS_DIR / f"{name}.yaml")


@pytest.fixture
def e2e_workspace(tmp_path):
    """Provide a clean workspace directory for E2E tests."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


@pytest.fixture
def scenarios_dir():
    """Return path to the scenarios directory."""
    return SCENARIOS_DIR
```

- [ ] **Step 2: Commit**

```bash
git add tests/e2e/conftest.py
git commit -m "test(e2e): add E2E conftest with scenario and workspace fixtures"
```

---

### Task 8: Create `tests/e2e/test_headless_lifecycle.py` — E2E test cases

**Files:**
- Create: `tests/e2e/test_headless_lifecycle.py`

- [ ] **Step 1: Write the E2E test module**

```python
"""Headless E2E lifecycle tests.

Runs the full LangGraph lifecycle with FakeStageTool.
Each test loads a YAML scenario and asserts final DB state.
"""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from story_lifecycle.db import models as db
from story_lifecycle.orchestrator import graph as graph_mod
from story_lifecycle.orchestrator.nodes import StoryState

from .runner import run_scenario, E2EResult
from .scenario import Scenario


SCENARIOS = Path(__file__).parent / "scenarios"


def _load(name: str) -> Scenario:
    return Scenario(SCENARIOS / f"{name}.yaml")


class TestHappyPath:
    """design → implement → test → completed"""

    def test_full_lifecycle(self, isolated_story_home, e2e_workspace):
        scenario = _load("happy_path")
        result = run_scenario(scenario, e2e_workspace)

        assert result.story is not None
        assert result.story["status"] == "completed"

        # Verify context has expected fields from design stage
        ctx = json.loads(result.story.get("context_json", "{}"))
        assert ctx.get("spec_path") == "docs/spec.md"
        assert ctx.get("complexity") == "S"

        # Verify events cover the full lifecycle
        event_types = [e["event_type"] for e in result.events]
        assert "plan" in event_types
        assert "execute" in event_types
        assert "complete" in event_types


class TestMarkdownDoneJson:
    """Done file is wrapped in markdown fences — robust_json_parse handles it."""

    def test_parses_markdown_json(self, isolated_story_home, e2e_workspace):
        scenario = _load("markdown_done_json")
        result = run_scenario(scenario, e2e_workspace)

        assert result.story is not None
        assert result.story["status"] == "completed"

        ctx = json.loads(result.story.get("context_json", "{}"))
        assert ctx.get("spec_path") == "docs/spec.md"
        assert "markdown" in ctx.get("summary", "").lower()


class TestMissingExpectedOutput:
    """Design stage omits required `spec_path` → story should end blocked."""

    def test_blocked_on_missing_field(self, isolated_story_home, e2e_workspace):
        scenario = _load("missing_expected_output")
        result = run_scenario(scenario, e2e_workspace)

        assert result.story is not None
        # The story should be blocked because spec_path is missing
        # from expected_outputs but not present in context
        assert result.story["status"] == "blocked"


class TestReviewRetryThenPass:
    """First review returns revise, second returns pass."""

    def test_retry_then_complete(self, isolated_story_home, e2e_workspace):
        scenario = _load("review_retry_then_pass")
        result = run_scenario(scenario, e2e_workspace)

        assert result.story is not None
        assert result.story["status"] == "completed"

        # Should have multiple execute events (retry)
        execute_events = [e for e in result.events if e["event_type"] == "execute"]
        assert len(execute_events) >= 2  # at least 2 executions for design stage

        # Should have review events
        review_events = [e for e in result.events if e["event_type"] == "review"]
        assert len(review_events) >= 1


class TestSubStoryWaitResume:
    """Parent story delegates to sub-stories, parent enters waiting_subtasks."""

    def test_parent_waits_for_children(self, isolated_story_home, e2e_workspace):
        scenario = _load("sub_story_wait_resume")
        key = scenario.story_key

        # Create parent story
        db.upsert_story(
            key,
            title=scenario.title,
            workspace=str(e2e_workspace),
            profile=scenario.profile,
            current_stage="design",
            status="active",
        )

        fake_tool = MagicMock()

        with (
            patch("story_lifecycle.orchestrator.nodes.planner") as mock_planner,
            patch("story_lifecycle.orchestrator.tools.get_tool") as mock_get_tool,
            patch("story_lifecycle.orchestrator.nodes.ttyd") as mock_ttyd,
            patch("story_lifecycle.orchestrator.nodes.notify"),
            patch("story_lifecycle.orchestrator.graph.emit_plan_done"),
            patch("story_lifecycle.orchestrator.graph.emit_terminal_opened"),
            patch("story_lifecycle.orchestrator.nodes.interrupt", side_effect=lambda x: None),
        ):
            # Planner returns a split decision
            mock_planner.is_available.return_value = True
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

            mock_get_tool.return_value = fake_tool
            mock_ttyd.session_name.return_value = f"story-{key}"
            mock_ttyd.session_alive.return_value = True
            mock_ttyd._MPLEX = None

            graph_mod._run_story_impl(key)

        # Verify parent is waiting_subtasks
        parent = db.get_story(key)
        assert parent is not None
        assert parent["status"] == "waiting_subtasks"

        # Verify sub-stories exist in DB
        sub_auth = db.get_story(f"{key}-auth")
        sub_api = db.get_story(f"{key}-api")
        assert sub_auth is not None
        assert sub_api["parent_key"] == key
        assert sub_api["status"] == "blocked"  # depends on auth

        # Verify delegation events
        delegate_events = [
            e for e in db.get_story_events(key) if e["event_type"] == "delegate"
        ]
        assert len(delegate_events) == 2
```

- [ ] **Step 2: Run all E2E tests to verify they pass**

Run: `python -m pytest tests/e2e/ -v`
Expected: All 5 tests pass.

- [ ] **Step 3: Run full test suite to verify no regressions**

Run: `python -m pytest tests/ -x -q`
Expected: All tests pass (existing + new E2E).

- [ ] **Step 4: Commit**

```bash
git add tests/e2e/test_headless_lifecycle.py
git commit -m "test(e2e): add 5 headless E2E lifecycle tests"
```

---

### Task 9: Fix any test failures and verify

**Files:**
- May modify: any file from previous tasks

- [ ] **Step 1: Run full suite**

Run: `python -m pytest tests/ -v`

If tests fail, investigate and fix. Common issues:

1. **Import errors** — ensure `tests/e2e/__init__.py` exists
2. **DB path conflicts** — ensure `isolated_story_home` fixture is applied to all E2E tests
3. **Graph state leaks** — ensure `_reset_graph_globals` autouse fixture runs
4. **interrupt() not patched** — ensure runner patches `nodes.interrupt`
5. **Profile not found** — ensure profile resolution works from test cwd or package root

- [ ] **Step 2: Verify lint passes**

Run: `python -m ruff check tests/e2e/`
Expected: No errors.

- [ ] **Step 3: Final commit (if fixes needed)**

```bash
git add -u
git commit -m "fix(e2e): address test failures from headless E2E integration"
```

---

## Self-Review Checklist

- [x] **Spec coverage:** Each of the 5 scenarios from the spec has a corresponding test
- [x] **Placeholder scan:** No TBD/TODO/vague instructions — all code is complete
- [x] **Type consistency:** Scenario class methods match usage in FakeStageTool and runner
- [x] **File paths:** All files use correct import paths matching the actual codebase structure
- [x] **Import paths:** `story_lifecycle.orchestrator.nodes.planner`, `story_lifecycle.orchestrator.tools.get_tool`, etc. verified against actual source
- [x] **No anti-tampering issues:** This is a test tool, not a web service — no Parameter Trust Analysis needed
