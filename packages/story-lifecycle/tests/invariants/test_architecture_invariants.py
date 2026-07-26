"""Centralized architecture invariant tests.

Maps to the six boundary rules in ``docs/module-architecture/02-modules-overview.md``:

1. ContextResolver 只读 / 零副作用  -> re-export T4.2 tests
2. Gate 是硬闸 (round_count > max_retries 强制 fail) -> re-export T1.1 tests
3. adapters <-> miner 通过 anchors.jsonl 文件契约通信 -> anchor round-trip
4. SOFT 缝 try/except 降级 -> re-export T4.3 tests
5. infra 零内部 import -> config.py / json_helpers.py only stdlib + yaml
6. HITL 是横切不是 stage -> (原 stage_library 契约随 phase6 死代码团删除)

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
