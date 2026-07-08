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
import sys
from pathlib import Path

import pytest

from story_lifecycle.knowledge.adapters.base import BaseAdapter
from story_lifecycle.orchestrator.engine.stage_library import (
    BUILTIN_STAGES,
    StageCategory,
    get_stage_definition,
)

# ── re-exports: keep original files runnable, centralize invariants here ──
from ..test_gate_hard_fail import (
    test_empty_findings_advance,
    test_high_findings_exceeding_max_retries_fail,
)
from ..test_resolver_pure import TestContextResolverPure
from ..test_soft_seam_degradation import (
    TestKnowledgeContextSoftSeam,
    TestTranscriptContextSoftSeam,
)


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
        stdlib_ok = {"__future__", "pathlib", "yaml", "json", "re"}
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
