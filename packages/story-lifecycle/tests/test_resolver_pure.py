"""T4.2 · ContextResolver 零副作用不变量测试。

架构不变量 #1:`resolver.py:3` 声明 "Pure read operations. No writes, no side effects"。
本测试用快照比对守护这条红线:
- 调用 `ContextResolver.resolve(story_key)` 前后,story DB 记录完全一致。
- `.story/` 工作区目录下所有文件的 mtime 不变、无新增文件。
- 多次调用结果一致(idempotent read)。
"""

import os
from pathlib import Path

from story_lifecycle.infra.db import models as db
from story_lifecycle.orchestrator.context.resolver import ContextResolver


def _snapshot_story_state(story_key: str) -> dict:
    """快照 story 的所有 DB 字段。"""
    story = db.get_story(story_key)
    assert story is not None
    return dict(story)


def _snapshot_files(workspace: Path) -> dict:
    """快照 .story/ 目录下所有文件的相对路径 -> mtime。"""
    story_dir = workspace / ".story"
    story_dir.mkdir(parents=True, exist_ok=True)
    snapshot = {}
    for root, _dirs, files in os.walk(story_dir):
        for name in files:
            p = Path(root) / name
            rel = p.relative_to(story_dir).as_posix()
            snapshot[rel] = p.stat().st_mtime_ns
    return snapshot


class TestContextResolverPure:
    def test_resolve_does_not_modify_story_db(self, isolated_story_home):
        """resolve 调用后 story DB 状态零变化。"""
        db.upsert_story(
            "S-RESOLVE",
            title="resolver test",
            workspace=str(isolated_story_home),
            profile="minimal",
            status="active",
            current_stage="design",
        )
        before = _snapshot_story_state("S-RESOLVE")

        resolver = ContextResolver()
        resolver.resolve("S-RESOLVE")
        resolver.resolve("S-RESOLVE")  # 多次调用

        after = _snapshot_story_state("S-RESOLVE")
        assert before == after

    def test_resolve_does_not_touch_workspace_files(self, isolated_story_home):
        """resolve 调用后 .story/ 目录无新增/修改/删除。"""
        story_dir = isolated_story_home / ".story"
        story_dir.mkdir(parents=True, exist_ok=True)
        # seed a file so the directory is non-empty
        seed = story_dir / "seed.txt"
        seed.write_text("seed", encoding="utf-8")

        db.upsert_story(
            "S-RESOLVE-FILES",
            title="resolver files test",
            workspace=str(isolated_story_home),
            profile="minimal",
            status="active",
            current_stage="design",
        )

        before = _snapshot_files(isolated_story_home)

        resolver = ContextResolver()
        resolver.resolve("S-RESOLVE-FILES")
        resolver.resolve("S-RESOLVE-FILES")

        after = _snapshot_files(isolated_story_home)
        assert before == after

    def test_resolve_returns_consistent_bundle(self, isolated_story_home):
        """多次 resolve 返回的 bundle 关键字段一致。"""
        db.upsert_story(
            "S-RESOLVE-IDEM",
            title="idem",
            workspace=str(isolated_story_home),
            profile="minimal",
            status="active",
            current_stage="design",
        )

        resolver = ContextResolver()
        b1 = resolver.resolve("S-RESOLVE-IDEM")
        b2 = resolver.resolve("S-RESOLVE-IDEM")

        assert b1.story["story_key"] == b2.story["story_key"] == "S-RESOLVE-IDEM"
        assert b1.revision == b2.revision
        assert b1.profile == b2.profile
        assert b1.projects == b2.projects
