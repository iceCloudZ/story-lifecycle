"""设计 10 改动 4.3:scenario 级 stale 检测(修订点 R5b:git 语义时间,不用 mtime)。"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from story_lifecycle.knowledge.knowledge_store.stale import (
    _git_last_change_ts,
    check_stale,
)


@pytest.fixture
def git_ws(tmp_path):
    """造一个 git 工作区:src 代码文件 + .story/knowledge(scenario + manifest)。"""
    ws = Path(tmp_path) / "ws"
    ws.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(ws), check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test"], cwd=str(ws), check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "test"], cwd=str(ws), check=True
    )

    # 代码文件(源)
    src = ws / "hc-order" / "src" / "OrderController.java"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("class OrderController {}\n", encoding="utf-8")

    # scenario 知识文件 + sidecar
    scenario = (
        ws / ".story" / "knowledge" / "scenarios" / "core-borrow" / "borrow.md"
    )
    scenario.parent.mkdir(parents=True, exist_ok=True)
    scenario.write_text(
        "---\n"
        "id: scenario:borrow-flow\n"
        "title: 借款放款流程\n"
        "source_refs: [hc-order/src/OrderController.java]\n"
        "verified_at: '2000-01-01T00:00:00+00:00'\n"
        "---\n"
        "# 借款放款流程\n",
        encoding="utf-8",
    )
    (Path(str(scenario) + ".json")).write_text(
        json.dumps({"apis": ["POST /api/loan"], "test_ref": "journeys/test_borrow.py"}),
        encoding="utf-8",
    )

    # manifest(source.commit = 当前 HEAD → 跳过 commit 检测,只测 scenario 层)
    manifest = ws / ".story" / "knowledge" / "manifest.yaml"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "status: active\nsource:\n  commit: PLACEHOLDER\n", encoding="utf-8"
    )

    subprocess.run(["git", "add", "-A"], cwd=str(ws), check=True)
    subprocess.run(
        ["git", "commit", "-qm", "init"], cwd=str(ws), check=True
    )
    head = (
        subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(ws), capture_output=True, text=True
        )
        .stdout.strip()
    )
    manifest.write_text(
        f"status: active\nsource:\n  commit: {head}\n", encoding="utf-8"
    )
    return ws


def test_git_last_change_ts_returns_epoch(git_ws):
    ts = _git_last_change_ts(git_ws, "hc-order/src/OrderController.java")
    assert isinstance(ts, int)
    # 2020 年以后创建的仓库,ct 应该 > 2020-01-01
    assert ts > 1577836800


def test_git_last_change_ts_none_for_untracked(git_ws):
    (git_ws / "untracked.txt").write_text("x", encoding="utf-8")
    assert _git_last_change_ts(git_ws, "untracked.txt") is None


def test_scenario_stale_by_code_change(git_ws):
    """verified_at(2000) 早于代码最后 git 变更 → 代码变更使 scenario 过期。"""
    result = check_stale(git_ws)
    assert result["stale"] is True
    assert result["scenarios"][0]["id"] == "scenario:borrow-flow"
    assert any("代码变更" in r for r in result["scenarios"][0]["reasons"])


def test_scenario_stale_by_journey_failure(git_ws):
    """last_status=FAIL(经 sidecar 回写)→ 绑定的 journey 最近失败使 scenario 过期。"""
    scenario_json = (
        git_ws / ".story" / "knowledge" / "scenarios" / "core-borrow" / "borrow.md.json"
    )
    data = json.loads(scenario_json.read_text(encoding="utf-8"))
    data["last_status"] = "FAIL"
    scenario_json.write_text(json.dumps(data), encoding="utf-8")
    # 重新生成 INDEX(KnowledgeIndex 读已有 INDEX 时不会重扫;删掉强制重生成)
    index_path = git_ws / ".story" / "knowledge" / "INDEX.json"
    if index_path.exists():
        index_path.unlink()
    result = check_stale(git_ws)
    assert result["stale"] is True
    reasons = result["scenarios"][0]["reasons"]
    assert any("journey 最近失败" in r for r in reasons)


def test_no_stale_when_verified_recent_and_pass(git_ws):
    """verified_at 晚于代码变更 + last_status=PASS → 不过期。"""
    scenario_json = (
        git_ws / ".story" / "knowledge" / "scenarios" / "core-borrow" / "borrow.md.json"
    )
    data = json.loads(scenario_json.read_text(encoding="utf-8"))
    data["last_status"] = "PASS"
    scenario_json.write_text(json.dumps(data), encoding="utf-8")
    scenario_md = (
        git_ws / ".story" / "knowledge" / "scenarios" / "core-borrow" / "borrow.md"
    )
    scenario_md.write_text(
        scenario_md.read_text(encoding="utf-8").replace(
            "verified_at: '2000-01-01T00:00:00+00:00'",
            f"verified_at: '{time.strftime('%Y-%m-%dT%H:%M:%S+00:00')}'",
        ),
        encoding="utf-8",
    )
    index_path = git_ws / ".story" / "knowledge" / "INDEX.json"
    if index_path.exists():
        index_path.unlink()
    result = check_stale(git_ws)
    assert result["stale"] is False


def test_commit_change_still_detected_first(git_ws):
    """commit 比对仍是第一层:改代码并 commit → 先报 commit 变化。"""
    src = git_ws / "hc-order" / "src" / "OrderController.java"
    src.write_text("class OrderController { void x() {} }\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(git_ws), check=True)
    subprocess.run(
        ["git", "commit", "-qm", "code change"], cwd=str(git_ws), check=True
    )
    result = check_stale(git_ws)
    assert result["stale"] is True
    assert "commit 变化" in result["reason"]
