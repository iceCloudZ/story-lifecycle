"""1.6 — 迁移脚本测试:done.json → story_doc 版本化记录。

设计依据:DESIGN §1.6。扫存量 story 的 .story/done/<key>/<stage>.json → upsert_story_doc。
旧 done.json 不删(miner 兼容期)。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from story_lifecycle.entry.cli.migrate_done_to_artifact import (
    migrate_done_to_artifact,
)


def _make_fake_db():
    db = MagicMock()
    db._docs = {}

    def _upsert(story_key, doc_type, content, change_reason, author="migration", title=""):
        v = db._docs.get((story_key, doc_type), 0) + 1
        db._docs[(story_key, doc_type)] = v
        return v

    db.upsert_story_doc.side_effect = _upsert
    return db


def _write_done(tmp_path, story_key, stage, payload):
    done_path = tmp_path / ".story" / "done" / story_key / f"{stage}.json"
    done_path.parent.mkdir(parents=True, exist_ok=True)
    done_path.write_text(json.dumps(payload), encoding="utf-8")
    return done_path


def test_migrate_reads_done_json_and_upserts_story_doc(tmp_path):
    db = _make_fake_db()
    _write_done(
        tmp_path,
        "OLD-1",
        "design",
        {
            "stage": "design",
            "status": "done",
            "summary": "旧设计",
            "spec_path": "story/spec.md",
            "files_changed": ["story/spec.md"],
        },
    )
    # 成果物文件存在(迁移优先读它进 content)
    spec = tmp_path / "story" / "spec.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text("# 旧设计方案\n正文\n", encoding="utf-8")

    result = migrate_done_to_artifact(str(tmp_path), db_module=db)
    assert result["scanned"] == 1
    assert result["migrated"] == 1
    assert db.upsert_story_doc.call_count == 1
    # design → spec doc_type
    _args = db.upsert_story_doc.call_args[0]
    assert _args[0] == "OLD-1"
    assert _args[1] == "spec"  # design maps to spec
    assert "旧设计方案" in _args[2]  # content 来自成果物文件


def test_migrate_falls_back_to_summary_when_no_artifact_file(tmp_path):
    db = _make_fake_db()
    _write_done(
        tmp_path,
        "OLD-2",
        "verify",
        {"stage": "verify", "summary": "测试报告摘要"},
    )
    result = migrate_done_to_artifact(str(tmp_path), db_module=db)
    assert result["migrated"] == 1
    _args = db.upsert_story_doc.call_args[0]
    # verify → test_report doc_type;content 兜底用 summary
    assert _args[1] == "test_report"
    assert "测试报告摘要" in _args[2]


def test_migrate_preserves_old_done_json(tmp_path):
    """红线(miner 兼容):迁移不删旧 done.json。"""
    db = _make_fake_db()
    done_path = _write_done(
        tmp_path, "OLD-3", "design", {"summary": "保留"}
    )
    migrate_done_to_artifact(str(tmp_path), db_module=db)
    assert done_path.exists(), "旧 done.json 必须保留(miner 兼容期)"


def test_migrate_dry_run_does_not_write(tmp_path):
    db = _make_fake_db()
    _write_done(tmp_path, "OLD-4", "design", {"summary": "x"})
    result = migrate_done_to_artifact(str(tmp_path), dry_run=True, db_module=db)
    assert result["dry_run"] is True
    assert result["migrated"] == 0
    db.upsert_story_doc.assert_not_called()
    assert all(d["action"] == "would_migrate" for d in result["details"])


def test_migrate_no_done_files(tmp_path):
    db = _make_fake_db()
    result = migrate_done_to_artifact(str(tmp_path), db_module=db)
    assert result["scanned"] == 0
    assert result["migrated"] == 0
    assert result["details"] == []


def test_migrate_multiple_stages_multiple_stories(tmp_path):
    db = _make_fake_db()
    _write_done(tmp_path, "S-A", "design", {"summary": "a设计"})
    _write_done(tmp_path, "S-A", "build", {"summary": "a编码"})
    _write_done(tmp_path, "S-B", "verify", {"summary": "b验证"})
    result = migrate_done_to_artifact(str(tmp_path), db_module=db)
    assert result["scanned"] == 3
    assert result["migrated"] == 3
    doc_types = {(d["story_key"], d["doc_type"]) for d in result["details"]}
    assert ("S-A", "spec") in doc_types
    assert ("S-A", "plan") in doc_types  # build → plan
    assert ("S-B", "test_report") in doc_types  # verify → test_report


def test_migrate_handles_malformed_done_json(tmp_path):
    """malformed done.json → payload={},用空 summary 兜底,不崩。"""
    db = _make_fake_db()
    done_path = tmp_path / ".story" / "done" / "BAD" / "design.json"
    done_path.parent.mkdir(parents=True, exist_ok=True)
    done_path.write_text("{这不是 json", encoding="utf-8")
    result = migrate_done_to_artifact(str(tmp_path), db_module=db)
    assert result["migrated"] == 1  # 仍迁移(content 兜底)
