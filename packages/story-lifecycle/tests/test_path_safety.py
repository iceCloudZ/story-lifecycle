"""Path-safety regression tests (P1 of the audit fix plan).

Covers the helpers added in ``infra/story_paths.py`` plus the trust-boundary
sanitization at the story-creation entry point and the swebench CLI. These are
the regression guards for the path-traversal / arbitrary-delete findings
(AI-1 #1/#4) and the upload-filename finding (AI-1 #3).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from story_lifecycle.infra.story_paths import (
    UnsafePathError,
    assert_within_workspace,
    safe_segment,
    safe_story_path,
)


# ── story_home (STORY_HOME 环境变量隔离) ──────────────────────────────────────


class TestStoryHome:
    def test_story_home_returns_path(self):
        from story_lifecycle.infra.paths import story_home

        assert isinstance(story_home(), Path)

    def test_story_home_uses_env_when_set(self, monkeypatch):
        from story_lifecycle.infra.paths import story_home

        monkeypatch.setenv("STORY_HOME", "/tmp/test-isolation")
        assert story_home() == Path("/tmp/test-isolation")

    def test_story_home_falls_back_to_home_dir(self, monkeypatch):
        from story_lifecycle.infra.paths import story_home

        monkeypatch.delenv("STORY_HOME", raising=False)
        assert story_home() == Path.home() / ".story-lifecycle"

    def test_story_home_env_isolation_end_to_end(self, tmp_path):
        """D1 核心验证：STORY_HOME 指向临时目录时，DB 与所有路径助手都落在那。

        回归：只有 3 处认 STORY_HOME 环境变量，其余 17 处硬编码
        Path.home()/.story-lifecycle → 测试隔离静默失效。用子进程验证（模块级
        常量在 import 时求值，子进程的 import 能看到新 env；同进程 reload 会
        破坏 dataclass 身份，污染其他测试）。
        """
        import os
        import subprocess
        import sys

        iso = tmp_path / "iso-home"
        code = (
            "import json, sys;"
            "import story_lifecycle.infra.db.models as db_mod;"
            "import story_lifecycle.infra.config as config_mod;"
            "import story_lifecycle.orchestrator.engine.planner as planner_mod;"
            "import story_lifecycle.orchestrator.engine.graph as graph_mod;"
            "import story_lifecycle.orchestrator.engine.profile_loader as pl_mod;"
            "print(json.dumps({"
            "'db': str(db_mod.get_db_path()),"
            "'config': str(config_mod.CONFIG_DIR),"
            "'planner': str(planner_mod.STORY_HOME),"
            "'graph': str(graph_mod.STORY_HOME),"
            "'profile_loader': str(pl_mod.STORY_HOME),"
            "}))"
        )
        env = dict(os.environ)
        env["STORY_HOME"] = str(iso)
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env=env,
        )
        assert proc.returncode == 0, proc.stderr
        homes = json.loads(proc.stdout.strip().splitlines()[-1])
        for name, path in homes.items():
            assert path.startswith(str(iso)), (
                f"{name} 必须落在 STORY_HOME 指向的目录（D1）：{path}"
            )

    def test_no_inline_story_home_outside_paths(self):
        """D1 静态守卫：.story-lifecycle 字面量只允许出现在 infra/paths.py。

        其余文件若有内联计算 = 隔离静默失效（新代码加了不认 env 的路径）。
        例外：infra/config.py 是架构不变量规定的 infra leaf（不得 import 内部
        模块），必须内联 —— 它认 STORY_HOME 环境变量，语义与 story_home 一致。
        """
        import story_lifecycle as _pkg

        pkg_root = Path(_pkg.__file__).resolve().parent
        offenders = []
        for path in pkg_root.rglob("*.py"):
            if path.name == "paths.py":
                continue
            if path.name == "config.py" and path.parent.name == "infra":
                continue  # infra leaf 不变量例外（内联但认 env）
            text = path.read_text(encoding="utf-8")
            # 忽略注释/docstring 行（历史引用），只看可执行代码
            for lineno, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if ".story-lifecycle" in stripped and "~/.story-lifecycle" not in stripped:
                    offenders.append(f"{path.relative_to(pkg_root)}:{lineno}")
        assert not offenders, (
            "以下文件内联 .story-lifecycle（应改用 infra.paths.story_home()）：\n"
            + "\n".join(offenders)
        )


# ── safe_segment ──────────────────────────────────────────────────────────────


class TestSafeSegment:
    def test_normal_key_passthrough(self):
        assert safe_segment("tapd-1234567890") == "tapd-1234567890"
        assert safe_segment("STORY-1065520") == "STORY-1065520"

    def test_traversal_collapsed_to_harmless_segment(self):
        # "../../etc" — separators are stripped so it can't escape as a path.
        # The key safety property: no "/" or "\" remains, and as a single
        # path segment it cannot traverse (assert_within_workspace enforces).
        cleaned = safe_segment("../../etc")
        assert "/" not in cleaned
        assert "\\" not in cleaned
        assert cleaned  # non-empty

    def test_path_separators_replaced(self):
        assert "/" not in safe_segment("a/b/c")
        assert "\\" not in safe_segment("a\\b\\c")

    def test_empty_falls_back(self):
        assert safe_segment("") == "story"
        assert safe_segment("   ") == "story"
        assert safe_segment("...") == "story"

    def test_shell_metachars_stripped(self):
        cleaned = safe_segment("story; rm -rf ~")
        assert ";" not in cleaned
        assert " " not in cleaned


# ── safe_story_path ───────────────────────────────────────────────────────────


class TestSafeStoryPath:
    def test_builds_nested_path(self, tmp_path):
        p = safe_story_path(tmp_path, ".story", "done", "tapd-123")
        assert p == tmp_path / ".story" / "done" / "tapd-123"

    def test_tainted_segment_cannot_escape(self, tmp_path):
        # A malicious story_key is collapsed before joining, so the result
        # never leaves the base directory.
        p = safe_story_path(tmp_path, ".story", "done", "../../etc")
        assert p.resolve().is_relative_to(tmp_path.resolve())
        # The traversal components are gone
        assert ".." not in p.parts

    def test_no_segments_returns_base(self, tmp_path):
        assert safe_story_path(tmp_path) == tmp_path


# ── assert_within_workspace ───────────────────────────────────────────────────


class TestAssertWithinWorkspace:
    def test_inside_ok(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        assert_within_workspace(sub, tmp_path)  # no raise

    def test_outside_raises(self, tmp_path):
        outside = tmp_path.parent / "sibling"
        with pytest.raises(UnsafePathError):
            assert_within_workspace(outside, tmp_path)

    def test_traversal_resolved_before_check(self, tmp_path):
        # Even with symlinks or ../ in the path, resolve() then relative_to
        # must catch the escape.
        sneaky = Path(tmp_path) / ".." / ".." / "etc"
        with pytest.raises(UnsafePathError):
            assert_within_workspace(sneaky, tmp_path)


# ── Entry-point sanitization ─────────────────────────────────────────────────


class TestEntryPointSanitization:
    """create_and_start_story must sanitize story_key at the trust boundary."""

    def test_malicious_key_sanitized_at_creation(
        self, tmp_path, isolated_story_home
    ):
        from story_lifecycle.orchestrator.service.story_service import (
            create_and_start_story,
        )
        from story_lifecycle.infra.db import models as db

        # A traversal key would historically let rmtree/targets escape.
        # After P1, it must be sanitized before any path concat.
        story_key = create_and_start_story(
            story_key="../../etc",
            title="t",
            profile="minimal",
            workspace=str(tmp_path),
        )
        # The returned key is the sanitized form — no path separators
        # (the key can no longer traverse, even if ".." appears as a substring
        # joined by hyphens, since there's no "/" to act on).
        assert "/" not in story_key
        assert "\\" not in story_key
        # And the row in DB uses the sanitized key
        row = db.get_story(story_key)
        assert row is not None
        assert row["story_key"] == story_key

    def test_done_dir_rmtree_refuses_escape(self, tmp_path):
        """Directly test the blast-shield: assert_within_workspace on done_dir.

        Even if a tainted key somehow reached the rmtree, the boundary check
        would refuse. We simulate this by calling the helper directly with a
        path that resolves outside.
        """
        # Build a path inside tmp_path that, if joined with ../.., escapes.
        # safe_segment cleans it first, so done_dir stays inside.
        from story_lifecycle.infra.story_paths import safe_story_path

        done_dir = safe_story_path(tmp_path, ".story", "done", "../../etc")
        # Must be inside tmp_path
        assert done_dir.resolve().is_relative_to(tmp_path.resolve())
        # And the boundary check passes (no raise)
        assert_within_workspace(done_dir, tmp_path)


# ── Upload filename sanitization (AI-1 #3) ───────────────────────────────────


class TestUploadFilenameSanitization:
    """api_intake_preview must take basename of upload.filename."""

    def test_basename_taken(self):
        # Mirror the logic added in api.py
        raw = "../../packages/story-lifecycle/src/__init__.py"
        safe_name = Path(raw).name
        assert safe_name == "__init__.py"
        assert ".." not in safe_name
        assert "/" not in safe_name

    def test_plain_filename_kept(self):
        assert Path("screenshot.png").name == "screenshot.png"

    def test_dotdot_dropped(self):
        # Names that reduce to .. after basename are rejected by the api loop
        raw = ".."
        safe_name = Path(raw).name
        assert safe_name in {".", ".."}  # api code drops these via `continue`


# ── swebench run_id / instance_id validation ─────────────────────────────────


class TestSwebenchIdValidation:
    """swebench --run-id must reject path-traversal values."""

    def test_validate_run_id_accepts_normal(self):
        from story_lifecycle.entry.cli.swebench import _SAFE_ID_RE

        assert _SAFE_ID_RE.match("run-2026-07-04")
        assert _SAFE_ID_RE.match("abc123")

    def test_validate_run_id_rejects_traversal(self):
        from story_lifecycle.entry.cli.swebench import _SAFE_ID_RE

        assert not _SAFE_ID_RE.match("../etc")
        assert not _SAFE_ID_RE.match("a/b")
        assert not _SAFE_ID_RE.match("")  # empty

    def test_validate_run_id_rejects_leading_dot(self):
        # Even if it matches the charset, leading "." is suspicious (relative)
        from story_lifecycle.entry.cli.swebench import _validate_run_id

        with pytest.raises(Exception):
            _validate_run_id(None, None, ".traversal")

    def test_assert_within_catches_escape(self, tmp_path):
        from story_lifecycle.entry.cli.swebench import _assert_within

        # Normal: inside
        _assert_within(tmp_path, "run-1", "inst-1")  # no raise

        # Escape attempt rejected
        with pytest.raises(UnsafePathError):
            _assert_within(tmp_path, "..", "..", "etc")


# ── rmtree blast-shield integration ──────────────────────────────────────────


class TestRmtreeBlastShield:
    """Direct test that reset_workspace (testing pkg) refuses escape."""

    def test_reset_workspace_with_normal_key(self, tmp_path):
        pytest.importorskip("testing")
        from testing.workspace import reset_workspace

        # Create a .story/done/<key> to be cleaned
        done = tmp_path / ".story" / "done" / "tapd-123"
        done.mkdir(parents=True)
        (done / "artifact.json").write_text("{}")

        reset_workspace(str(tmp_path), "tapd-123")
        assert not done.exists()

    def test_reset_workspace_tainted_key_stays_in_workspace(self, tmp_path):
        pytest.importorskip("testing")
        from testing.workspace import reset_workspace

        # Create a sibling dir outside the .story tree we DON'T want touched
        outside_victim = tmp_path / "victim"
        outside_victim.mkdir()
        (outside_victim / "important.txt").write_text("keep me")

        # Sanitization collapses "../../victim" → a harmless single segment
        # that lands INSIDE .story/done/ — NOT tmp_path/victim.
        reset_workspace(str(tmp_path), "../../victim")
        assert outside_victim.exists()  # untouched
        assert (outside_victim / "important.txt").read_text() == "keep me"
