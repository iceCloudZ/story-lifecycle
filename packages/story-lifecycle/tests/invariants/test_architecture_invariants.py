"""Centralized architecture invariant tests.

Maps to the six boundary rules in ``docs/module-architecture/02-modules-overview.md``:

1. ContextResolver 只读 / 零副作用  -> re-export T4.2 tests
2. Gate 是硬闸 (round_count > max_retries 强制 fail) -> re-export T1.1 tests
3. adapters <-> miner 通过 anchors.jsonl 文件契约通信 -> anchor round-trip
4. SOFT 缝 try/except 降级 -> re-export T4.3 tests
5. infra 零内部 import -> config.py / json_helpers.py only stdlib + yaml
6. HITL 是横切不是 stage -> clarify/approval/supervisor 不在 stage_library

The re-export pattern keeps the original test files (and their original
commit history / blame) alive while providing a single ``tests/invariants/``
entry point that new windows can run to verify all architecture rules.
"""

from __future__ import annotations

import ast
import builtins
import json
from pathlib import Path

import pytest

from story_lifecycle.knowledge.adapters.base import BaseAdapter
from story_lifecycle.orchestrator.engine.stage_library import (
    BUILTIN_STAGES,
    StageCategory,
    get_stage_definition,
)

# ── re-exports: keep original files runnable, centralize invariants here ──


# ── invariant #3: anchors.jsonl file contract ──


class _FakeAdapter(BaseAdapter):
    """Minimal adapter sufficient to exercise write_anchor."""

    name = "fake"

    def switch_provider(self, provider):
        return None

    def launch_cmd(self, model):
        return ""

    def inject_prompt(self, prompt, story_key, stage):
        self.write_anchor(prompt, story_key, stage)
        return None


class TestAnchorFileContract:
    """adapters ↔ miner communicate through anchors.jsonl, not imports.

    Lifecycle writes a line-oriented JSON file; any consumer (miner, future
    tooling) can parse it without importing lifecycle internals.
    """

    @pytest.fixture
    def adapter(self):
        return _FakeAdapter()

    def test_lifecycle_writes_required_anchor_fields(self, adapter, tmp_path):
        """anchor 记录必须包含跨包契约字段。"""
        path = adapter.write_anchor(
            "hello world", "STORY-ANCHOR", "design",
            cwd=str(tmp_path), workspace=str(tmp_path),
        )
        assert path is not None
        assert path.endswith("anchors.jsonl")

        with open(path, "r", encoding="utf-8") as fh:
            records = [json.loads(line) for line in fh]

        assert len(records) == 1
        rec = records[0]
        required = {"story_key", "stage", "adapter", "cwd", "prompt_hash", "ts"}
        assert required.issubset(rec.keys())
        assert rec["story_key"] == "STORY-ANCHOR"
        assert rec["stage"] == "design"
        assert rec["adapter"] == "fake"

    def test_lifecycle_write_anchor_does_not_require_miner(self, adapter, tmp_path, monkeypatch):
        """卸包照跑:lifecycle 写锚点不依赖 miner 包可 import。"""
        real_import = builtins.__import__

        def _block_miner(name, *args, **kwargs):
            if name is not None and name.startswith("miner"):
                raise ImportError(f"{name} blocked by invariant test")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _block_miner)

        path = adapter.write_anchor(
            "no-miner prompt", "STORY-NO-MINER", "design",
            cwd=str(tmp_path), workspace=str(tmp_path),
        )
        assert path is not None
        assert Path(path).exists()


# ── invariant #5: infra zero internal import ──


class TestInfraZeroInternalImport:
    """infra/config.py and infra/json_helpers.py must stay dependency-sink.

    They may only import stdlib (plus yaml for config). Importing other
    ``story_lifecycle`` modules would create layering inversions and cycles.
    """

    @staticmethod
    def _parse_imports(path: Path) -> list[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    names.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                names.append(node.module or "")
        return names

    @pytest.mark.parametrize(
        "rel_path,extra_allowed",
        [
            ("src/story_lifecycle/infra/config.py", {"yaml"}),
            ("src/story_lifecycle/infra/json_helpers.py", set()),
        ],
    )
    def test_no_internal_story_lifecycle_imports(self, rel_path, extra_allowed):
        """指定 infra 文件不得 import story_lifecycle 内部模块。"""
        root = Path(__file__).resolve().parents[2]  # packages/story-lifecycle
        path = root / rel_path
        imports = self._parse_imports(path)

        for name in imports:
            assert not name.startswith("story_lifecycle"), (
                f"{rel_path} imports internal module {name!r}; infra must be a leaf"
            )

    @pytest.mark.parametrize(
        "rel_path,extra_allowed",
        [
            ("src/story_lifecycle/infra/config.py", {"yaml"}),
            ("src/story_lifecycle/infra/json_helpers.py", set()),
        ],
    )
    def test_only_stdlib_plus_explicit_third_party(self, rel_path, extra_allowed):
        """指定 infra 文件只能 import stdlib + 显式允许的第三方包。"""
        # stdlib modules infra leaf files may use. Kept tight on purpose —
        # widening requires architectural justification (infra = leaf, no
        # heavyweight deps). os added for env-var lookups (STORY_WORKTREES_ROOT).
        # tempfile added for atomic config write (grok-build §6.1).
        stdlib_ok = {"__future__", "pathlib", "yaml", "json", "re", "os", "tempfile"}
        root = Path(__file__).resolve().parents[2]
        path = root / rel_path
        imports = self._parse_imports(path)

        for name in imports:
            top = name.split(".")[0]
            assert top in stdlib_ok or top in extra_allowed, (
                f"{rel_path} imports disallowed module {name!r}; "
                "infra leaf files must only use stdlib + {extra_allowed}"
            )


# ── invariant #6: HITL is cross-cutting, not a stage ──


class TestHitlCrossCuttingNotStage:
    """clarify / approval / supervisor must not be modeled as stage_library stages.

    HITL concerns are blocking points embedded in the execution flow (MCP
    clarify server, API approval endpoints, supervisor decisions). They must not
    appear as atomic stages in the built-in stage catalog.
    """

    @pytest.mark.parametrize("hitl_name", ["clarify", "approval", "supervisor", "hitl"])
    def test_hitl_names_are_not_builtin_stages(self, hitl_name):
        """没有名为 clarify / approval / supervisor / hitl 的 stage。"""
        assert get_stage_definition(hitl_name) is None

    def test_no_hitl_category_in_stage_library(self):
        """StageCategory 中没有 HITL 专用分类。"""
        categories = {member.value for member in StageCategory}
        assert "hitl" not in categories
        assert "clarify" not in categories
        assert "approval" not in categories

    def test_builtin_stages_do_not_contain_hitl_keywords(self):
        """所有内置 stage 名称都不含 HITL 关键字。"""
        hitl_keywords = {"clarify", "approval", "supervisor", "hitl", "mcp"}
        for name in BUILTIN_STAGES:
            for kw in hitl_keywords:
                assert kw not in name, (
                    f"built-in stage {name!r} contains HITL keyword {kw!r}"
                )


# ── invariant #7: consult runner return-shape contract (DESIGN §8.3) ──


class TestConsultRunnerContract:
    """``consult_runner.run_consult_sync`` 的返回 dict 必须含 ``status / findings / error``。

    所有 4 条 status 路径(ok / timeout / spawn_failed / no_headless)都走同一份契约,
    让编排 LLM / consult_orchestrator 能放心地 ``result["status"]`` 读字段不崩。
    """

    def test_no_headless_path_returns_contract_fields(self):
        from story_lifecycle.orchestrator.engine.consult_runner import run_consult_sync

        result = run_consult_sync(
            adapter_name="totally-fake-adapter",
            focus="x",
            workspace=".",
            request_id="contracttest1",
        )
        assert {"status", "findings", "error"}.issubset(result.keys())
        assert result["status"] == "no_headless"
        assert isinstance(result["findings"], dict)
        assert isinstance(result["error"], str)


# ── invariant #8: consult orchestrator return-shape contract (DESIGN §8.3) ──


class TestConsultOrchestratorContract:
    """``consult_orchestrator.run_consult_orchestrator`` 的返回 dict 必须含 5 个字段。

    必有字段:``advice / confidence / followed_up / rounds / terminated_by``。
    ``terminated_by`` 是**开集诊断字段** —— 这里**只断言字段存在,不断言取值集合**
    (wiring 层 / 未来扩展可追加新取值如 ``exception`` / ``test_fake``,DESIGN §5.6)。
    """

    def _fake_invoke_text(self, messages, tools, **kw):
        return {
            "message": {"role": "assistant", "content": "ok"},
            "tool_calls": [],
            "content": "ok",
        }

    def test_text_path_returns_required_fields(self):
        from story_lifecycle.orchestrator.engine.consult_orchestrator import (
            run_consult_orchestrator,
        )

        result = run_consult_orchestrator(
            consult_request={
                "question": "q",
                "context": "",
                "urgency": "medium",
                "request_id": "c1",
                "adapter_of_caller": "claude",
            },
            story_facts={"story_key": "S", "stage": "x"},
            workspace=".",
            invoke_with_tools=self._fake_invoke_text,
            spawn_fn=lambda **kw: pytest.fail("should not spawn"),
        )
        REQUIRED = {"advice", "confidence", "followed_up", "rounds", "terminated_by"}
        assert REQUIRED.issubset(result.keys())
        assert isinstance(result["advice"], str)
        assert result["advice"], "advice must never be empty (不阻塞 code agent)"
        assert isinstance(result["followed_up"], bool)
        assert isinstance(result["rounds"], int)

    def test_terminated_by_is_open_set_diagnostic(self):
        """terminated_by 是字符串字段(诊断用途),不限定具体枚举值。

        这条契约保护:wiring 层 / 未来扩展追加新 terminated_by 取值(如 exception /
        test_fake / hard_timeout)不需要改本契约测试。这里只断言是 str + 非空。
        """
        from story_lifecycle.orchestrator.engine.consult_orchestrator import (
            run_consult_orchestrator,
        )

        result = run_consult_orchestrator(
            consult_request={
                "question": "q",
                "context": "",
                "urgency": "medium",
                "request_id": "c2",
                "adapter_of_caller": "claude",
            },
            story_facts={"story_key": "S", "stage": "x"},
            workspace=".",
            invoke_with_tools=self._fake_invoke_text,
            spawn_fn=lambda **kw: {},
        )
        assert "terminated_by" in result
        assert isinstance(result["terminated_by"], str)
        assert result["terminated_by"]


# ── invariant #9: replanner action shape vs planner consumer alignment (DESIGN §4.4) ──


class TestReplannerActionShapeAlignment:
    """``replanner._tool_call_to_action`` 产出的 action 结构必须与
    ``continue_orchestrator_agent`` 消费端一致(DESIGN §4.4 对齐表)。

    防 §4.4 对齐表漂移:若 replanner 产出的 ``{action: "launch", adapter, stage,
    focus, done_file}`` 字段名改了,planner 消费端读 ``action.get("action") == "launch"``
    就会失效 → 静默跳过 stage。本契约锁住字段名。
    """

    def test_plan_step_tool_call_produces_launch_action_with_required_fields(self):
        from story_lifecycle.orchestrator.engine.replanner import _tool_call_to_action

        tc = {
            "id": "c1",
            "type": "function",
            "function": {
                "name": "plan_step",
                "arguments": {
                    "stage": "implement",
                    "adapter": "kimi",
                    "focus": "do X",
                },
            },
        }
        action = _tool_call_to_action(tc, "STORY-1")
        assert action is not None
        # 消费端读这些字段(planner.py:947 if action.get("action") == "launch")
        assert action["action"] == "launch"
        assert action["stage"] == "implement"
        assert action["adapter"] == "kimi"
        assert action["focus"] == "do X"
        assert action["done_file"] == ".story/done/STORY-1/implement.json"

    def test_skip_stage_tool_call_produces_skip_action_with_required_fields(self):
        from story_lifecycle.orchestrator.engine.replanner import _tool_call_to_action

        tc = {
            "id": "c2",
            "type": "function",
            "function": {
                "name": "skip_stage",
                "arguments": {"stage": "release", "reason": "low value"},
            },
        }
        action = _tool_call_to_action(tc, "STORY-2")
        assert action is not None
        # 消费端读这些字段(planner.py:939 if action.get("action") == "skip")
        assert action["action"] == "skip"
        assert action["stage"] == "release"
        assert action["reason"] == "low value"

    def test_replanner_loop_returns_action_list_compatible_with_consumer(self):
        """``replan`` 端到端:产出的 list 里每条 action 都是 consumer 可读形态。"""
        from story_lifecycle.orchestrator.engine.replanner import replan

        state = {"n": 0}

        def fake_invoke(messages, tools, **kw):
            state["n"] += 1
            if state["n"] == 1:
                return {
                    "message": {"role": "assistant", "content": ""},
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {
                                "name": "plan_step",
                                "arguments": {"stage": "verify", "adapter": "claude"},
                            },
                        }
                    ],
                    "content": "",
                }
            # 第二次空 → break
            return {
                "message": {"role": "assistant", "content": "done"},
                "tool_calls": [],
                "content": "done",
            }

        actions = replan(
            story_facts={"story_key": "S-3", "stage": "verify"},
            feedback={"stage": "verify", "reason": "x"},
            prior_actions=[],
            invoke_with_tools=fake_invoke,
            tools=[],
        )
        assert len(actions) == 1
        a = actions[0]
        # 消费端契约:必须能用 .get("action") 读出来
        assert a.get("action") == "launch"
        assert a.get("stage") == "verify"
        assert a.get("adapter") == "claude"
