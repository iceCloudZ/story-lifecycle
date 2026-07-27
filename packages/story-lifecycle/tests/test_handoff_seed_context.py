"""接手中途需求(handoff)— seed_context 端到端测试。

覆盖链路:
  前端 → POST /api/story/{key}/start (seed_context)
       → context_json.seed_context
       → 规划 LLM (run_orchestrator_agent 读 seed_context)
       → 执行 prompt (_build_cli_prompt 的 "### 已有工作(接手)" section)

另含 BUG FIX 回归(2026-07-27):run_orchestrator_agent 原读不存在的 story.content 列
→ 永远空串 → 规划 LLM 从来看不到 PRD 正文。改成从 context_json.prd_path 读文件。
"""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Isolated DB fixture(镜像 test_agent_planner 的同名 fixture)。"""
    from story_lifecycle.infra.db import models as db

    db_path = tmp_path / "story.db"
    monkeypatch.setattr(db, "get_db_path", lambda: db_path)
    db.init_db()
    return db


@pytest.fixture
def api_client(isolated_db, monkeypatch, tmp_path):
    """FastAPI TestClient + 隔离 DB + 关掉真 LLM/executor。"""
    from story_lifecycle.orchestrator.service.api import app
    from fastapi.testclient import TestClient

    # 阻止 start_story_async 真起线程(/start 不触发执行,但保险)
    monkeypatch.setattr(
        "story_lifecycle.orchestrator.service.api.start_story_async",
        lambda *a, **kw: None,
    )
    return TestClient(app)


def _seed_story(isolated_db, tmp_path, *, seed_context="", prd_path="", **overrides):
    """建一个 ready/active story + workspace,可选预置 context_json 字段。"""
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    defaults = {
        "story_key": "HANDOFF-001",
        "title": "接手中途需求测试",
        "profile": "single-pass",
        "workspace": str(workspace),
        "status": "active",
        "intake_state": "ready",
        "source_type": "",
        "source_id": "",
    }
    defaults.update(overrides)
    isolated_db.upsert_story(**defaults)
    # 预置 context_json(模拟 /start 之前可能已有字段)
    if seed_context or prd_path:
        ctx = {}
        if seed_context:
            ctx["seed_context"] = seed_context
        if prd_path:
            ctx["prd_path"] = prd_path
        isolated_db.update_story(defaults["story_key"], context_json=json.dumps(ctx))
    return defaults


# ============================================================
# 端点测试:POST /api/story/{key}/start
# ============================================================


class TestStartEndpointPersistsSeedContext:
    def test_seed_context_written_to_context_json(
        self, api_client, isolated_db, monkeypatch, tmp_path
    ):
        """/start 带 seed_context → context_json.seed_context 有值。"""
        _seed_story(isolated_db, tmp_path)

        resp = api_client.post(
            "/api/story/HANDOFF-001/start",
            json={"content": "PRD正文", "seed_context": "已有spec,代码半成品,差测试"},
        )

        assert resp.status_code == 200, resp.text
        story = isolated_db.get_story("HANDOFF-001")
        ctx = json.loads(story["context_json"])
        assert ctx["seed_context"] == "已有spec,代码半成品,差测试"

    def test_empty_seed_context_not_written(self, api_client, isolated_db, tmp_path):
        """seed_context 空 → context_json 不含 seed_context 键(不污染 context)。"""
        _seed_story(isolated_db, tmp_path)

        resp = api_client.post(
            "/api/story/HANDOFF-001/start",
            json={"content": "PRD正文", "seed_context": ""},
        )

        assert resp.status_code == 200
        story = isolated_db.get_story("HANDOFF-001")
        ctx = json.loads(story["context_json"])
        assert "seed_context" not in ctx

    def test_seed_context_fallback_as_prd_when_no_content(
        self, api_client, isolated_db, tmp_path
    ):
        """接手模式没填 PRD(只填 seed_context)→ 后端用 seed_context 兜底当 PRD 正文。

        否则 _prepare_intake_prd_content 会返回 content_required 409 挡住 handoff。
        """
        _seed_story(isolated_db, tmp_path)

        resp = api_client.post(
            "/api/story/HANDOFF-001/start",
            json={"content": "", "seed_context": "已有spec,差测试和验收"},
        )

        assert resp.status_code == 200, resp.text
        # PRD 文件被创建(用 seed_context 当正文)
        story = isolated_db.get_story("HANDOFF-001")
        ctx = json.loads(story["context_json"])
        assert ctx.get("prd_path"), "PRD 文件路径应已写入"
        prd_text = Path(ctx["prd_path"]).read_text(encoding="utf-8")
        assert "已有spec" in prd_text


# ============================================================
# 规划 LLM:run_orchestrator_agent 读 context_json
# ============================================================


class TestRunOrchestratorAgentReadsContext:
    """BUG FIX(2026-07-27):run_orchestrator_agent 原读 story.content(不存在列)→ 永远空。

    改成从 context_json 读 seed_context + prd_path(→ PRD 文件摘录)。
    """

    def test_seed_context_passed_to_planning_llm(
        self, isolated_db, monkeypatch, tmp_path
    ):
        """story context_json 含 seed_context → 传给 LLM 的 user_msg 含接手说明。"""
        _seed_story(isolated_db, tmp_path, seed_context="做到一半,差验收")

        mock_llm = MagicMock()
        mock_llm.api_key = "fake"
        mock_llm.invoke_structured.return_value = MagicMock(stages=[])

        with patch(
            "story_lifecycle.orchestrator.engine.planner.get_llm",
            return_value=mock_llm,
        ):
            from story_lifecycle.orchestrator.engine.planner import (
                run_orchestrator_agent,
            )

            run_orchestrator_agent("HANDOFF-001")

        # invoke_structured 的 prompt 参数应含 seed_context
        call_args = mock_llm.invoke_structured.call_args
        prompt = call_args.kwargs.get("prompt") or (
            call_args.args[0] if call_args.args else ""
        )
        assert "做到一半" in prompt
        assert "接手说明" in prompt

    def test_prd_content_passed_to_planning_llm_bugfix(
        self, isolated_db, monkeypatch, tmp_path
    ):
        """PRD 文件存在 → 规划 LLM 的 user_msg 含 PRD 内容(修 bug 前永远空串)。

        回归测试:planner.py:328 原 content = story.get("content","") 读不存在的列。
        """
        prd_file = tmp_path / "prd.md"
        prd_file.write_text("# 需求\n实现提额消息事件中心", encoding="utf-8")
        _seed_story(isolated_db, tmp_path, prd_path=str(prd_file))

        mock_llm = MagicMock()
        mock_llm.api_key = "fake"
        mock_llm.invoke_structured.return_value = MagicMock(stages=[])

        with patch(
            "story_lifecycle.orchestrator.engine.planner.get_llm",
            return_value=mock_llm,
        ):
            from story_lifecycle.orchestrator.engine.planner import (
                run_orchestrator_agent,
            )

            run_orchestrator_agent("HANDOFF-001")

        call_args = mock_llm.invoke_structured.call_args
        prompt = call_args.kwargs.get("prompt") or (
            call_args.args[0] if call_args.args else ""
        )
        # PRD 正文应进 prompt(修 bug 前这里会 fail)
        assert "实现提额消息事件中心" in prompt

    def test_no_prd_no_seed_context_still_works(
        self, isolated_db, monkeypatch, tmp_path
    ):
        """无 PRD 无 seed_context → 规划仍正常(基于 title),不崩。"""
        _seed_story(isolated_db, tmp_path)  # 无 seed/prd

        mock_llm = MagicMock()
        mock_llm.api_key = "fake"
        mock_llm.invoke_structured.return_value = MagicMock(stages=[])

        with patch(
            "story_lifecycle.orchestrator.engine.planner.get_llm",
            return_value=mock_llm,
        ):
            from story_lifecycle.orchestrator.engine.planner import (
                run_orchestrator_agent,
            )

            result = run_orchestrator_agent("HANDOFF-001")

        assert result["status"] == "planning"


# ============================================================
# _build_agent_user_message 单元测试
# ============================================================


class TestBuildAgentUserMessage:
    def test_seed_context_in_message(self):
        from story_lifecycle.orchestrator.engine.planner import (
            _build_agent_user_message,
        )

        msg = _build_agent_user_message(
            story_key="S-1",
            title="接手测试",
            content="",
            seed_context="已有 spec,代码半成品",
        )
        assert "接手测试" in msg
        assert "已有 spec" in msg
        assert "接手说明" in msg

    def test_content_and_seed_coexist(self):
        """PRD content + seed_context 共存(两个独立 section)。"""
        from story_lifecycle.orchestrator.engine.planner import (
            _build_agent_user_message,
        )

        msg = _build_agent_user_message(
            story_key="S-1",
            title="T",
            content="PRD正文内容",
            seed_context="接手说明内容",
        )
        assert "PRD正文内容" in msg
        assert "接手说明内容" in msg
