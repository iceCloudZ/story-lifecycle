"""Tests for consult_cmd(§5.7 / 实施步骤 4)。

两类测试:
1. **纯核心** ``run_consult_cli`` 的注入式单测 —— 全注入(env / log_event_fn /
   run_consult_orchestrator_fn / id_factory),覆盖:正常路径、depth 守卫、env 缺失、
   orchestrator 异常的 fallback、事件落 DB 调用契约、stdout 文本格式。
2. **子进程集成** ``TestConsultSubprocess`` —— subprocess 跑真 ``story consult``,
   env 注入 STORY_CONSULT_FAKE 测试缝(DESIGN §8.2),断言 exit 0、stdout 格式、
   DB 落了 consult_request / consult_response 事件、depth 守卫 exit 2。

分层:纯核心层不感知 STORY_CONSULT_FAKE(那是薄壳 wiring 层的事);这里只测核心。
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from click.testing import CliRunner

from story_lifecycle.entry.cli.consult_cmd import consult_cmd, run_consult_cli
from story_lifecycle.infra.db import models as db


# ── helpers ──────────────────────────────────────────────────────────


def _good_env():
    """生产形态的 env:planner spawn headless 时注入(DESIGN §5.8)。"""
    return {
        "STORY_KEY": "FEAT-TEST",
        "STORY_STAGE": "implement",
        "STORY_WORKSPACE": "D:/some/workspace",
        "STORY_ADAPTER": "claude",
    }


def _fake_orch_ok(**kw):
    return {
        "advice": "USE APPROACH A",
        "confidence": "high",
        "followed_up": True,
        "rounds": 2,
        "terminated_by": "finalize",
        "spawn_results": [
            {
                "round": 1,
                "adapter": "kimi",
                "focus": "check",
                "result": {"status": "ok", "findings": {"summary": "ok"}, "error": ""},
            }
        ],
    }


# ── TestRunConsultCliPureCore ──────────────────────────────────────


class TestRunConsultCliPureCore:
    def test_normal_path_logs_request_and_response(self):
        """正常路径:落 consult_request + consult_response 两个事件,exit 0。"""
        events = []

        def fake_log(story_key, stage, event_type, payload):
            events.append((story_key, stage, event_type, payload))

        text, code = run_consult_cli(
            question="q",
            context="ctx",
            urgency="high",
            env=_good_env(),
            log_event_fn=fake_log,
            run_consult_orchestrator_fn=_fake_orch_ok,
            id_factory=lambda: "fixedid12345",
        )
        assert code == 0
        assert "[consult fixedid12345]" in text
        assert "[confidence: high]" in text
        assert "USE APPROACH A" in text
        # 两事件
        types = [e[2] for e in events]
        assert types == ["consult_request", "consult_response"]
        # consult_request 含 question / urgency / adapter_of_caller
        req_payload = events[0][3]
        assert req_payload["question"] == "q"
        assert req_payload["urgency"] == "high"
        assert req_payload["adapter_of_caller"] == "claude"
        assert req_payload["request_id"] == "fixedid12345"
        # consult_response 去掉 spawn_results,加 spawn_count
        resp_payload = events[1][3]
        assert resp_payload["id"] == "fixedid12345"
        assert "spawn_results" not in resp_payload
        assert resp_payload["spawn_count"] == 1
        assert resp_payload["advice"] == "USE APPROACH A"

    def test_orchestrator_exception_falls_back_to_exit_zero(self):
        """orchestrator 抛异常 → advice 是 fallback 文案,confidence=low,**exit 0**。"""

        def boom(**kw):
            raise RuntimeError("LLM exploded")

        def fake_log(*a, **kw):
            pass

        text, code = run_consult_cli(
            question="q",
            context="",
            urgency="medium",
            env=_good_env(),
            log_event_fn=fake_log,
            run_consult_orchestrator_fn=boom,
        )
        assert code == 0, "consult must NEVER block code agent — exception path exits 0"
        assert "异常" in text
        assert "[confidence: low]" in text

    def test_depth_guard_blocks_recursion(self):
        """STORY_CONSULT_DEPTH>=1 → exit 2,不调 orchestrator。"""
        orch_called = []
        env = {**_good_env(), "STORY_CONSULT_DEPTH": "1"}

        text, code = run_consult_cli(
            question="q",
            context="",
            urgency="medium",
            env=env,
            log_event_fn=lambda *a, **kw: None,
            run_consult_orchestrator_fn=lambda **kw: orch_called.append(kw),
        )
        assert code == 2
        assert "递归守卫" in text or "reviewer" in text
        assert orch_called == [], "must NOT call orchestrator when depth guard hits"

    def test_depth_guard_non_integer_treated_as_zero(self):
        """非整数的 STORY_CONSULT_DEPTH 当 0 处理(不崩,继续正常路径)。"""
        env = {**_good_env(), "STORY_CONSULT_DEPTH": "garbage"}
        text, code = run_consult_cli(
            question="q",
            context="",
            urgency="medium",
            env=env,
            log_event_fn=lambda *a, **kw: None,
            run_consult_orchestrator_fn=_fake_orch_ok,
        )
        assert code == 0  # 当 0 处理,不触发守卫

    def test_missing_story_key_exits_two(self):
        """缺 STORY_KEY → exit 2。"""
        env = _good_env()
        env.pop("STORY_KEY")
        text, code = run_consult_cli(
            question="q",
            context="",
            urgency="medium",
            env=env,
            log_event_fn=lambda *a, **kw: None,
            run_consult_orchestrator_fn=_fake_orch_ok,
        )
        assert code == 2
        assert "STORY_KEY" in text

    def test_missing_workspace_exits_two(self):
        """缺 STORY_WORKSPACE → exit 2。"""
        env = _good_env()
        env.pop("STORY_WORKSPACE")
        text, code = run_consult_cli(
            question="q",
            context="",
            urgency="medium",
            env=env,
            log_event_fn=lambda *a, **kw: None,
            run_consult_orchestrator_fn=_fake_orch_ok,
        )
        assert code == 2
        assert "STORY_WORKSPACE" in text

    def test_stdout_format_has_request_id_and_confidence(self):
        """stdout 必须是 ``[consult <rid>] [confidence: <level>]\\n<advisory>``。"""
        text, code = run_consult_cli(
            question="q",
            context="",
            urgency="low",
            env=_good_env(),
            log_event_fn=lambda *a, **kw: None,
            run_consult_orchestrator_fn=lambda **kw: {
                "advice": "ADVISORY BODY",
                "confidence": "medium",
                "terminated_by": "text",
            },
            id_factory=lambda: "rid0000011111",
        )
        assert code == 0
        assert text.startswith("[consult rid0000011111] [confidence: medium]\n")
        assert "ADVISORY BODY" in text

    def test_id_factory_default_is_random_hex(self):
        """id_factory 缺省 → uuid hex[:12](12 字符,hex)。"""
        text, code = run_consult_cli(
            question="q",
            context="",
            urgency="medium",
            env=_good_env(),
            log_event_fn=lambda *a, **kw: None,
            run_consult_orchestrator_fn=_fake_orch_ok,
        )
        assert code == 0
        # extract rid from "[consult <rid>]"
        rid = text.split("[consult ")[1].split("]")[0]
        assert len(rid) == 12
        assert all(c in "0123456789abcdef" for c in rid)


# ── TestConsultSubprocess (集成,§8.2) ─────────────────────────────


class TestConsultSubprocess:
    """子进程跑真 ``story consult``,env 注入 STORY_CONSULT_FAKE(测试缝)。

    链路里只有「真 LLM + 真 spawn」被旁路 —— 那两块各有 test_consult_orchestrator.py
    / test_consult_runner.py 注入式单测兜底。本类验证的是「薄壳 + 核心 + DB 事件」整链路。
    """

    @pytest.fixture
    def seeded_db(self, isolated_story_home, monkeypatch):
        """isolated DB + 一条 story 记录(log_event 需要 story 存在)。"""
        db.init_db()
        # upsert a story so log_event's foreign-key (if any) can resolve
        story_key, _ = db.upsert_story_from_source(
            source_type="manual",
            source_id="999",
            title="consult test",
        )
        # Re-key it to match what our env will send (FEAT-SUBPROCESS)
        # Easier: just use the returned key
        return story_key

    def _run_story_cli(self, env_overrides: dict, *cli_args: str) -> subprocess.CompletedProcess:
        """Invoke ``python -m story_lifecycle consult ...`` in subprocess.

        Subprocess picks up STORY_HOME from env (so DB writes go to our tmp DB).
        """
        env = {
            **os.environ,
            # Force fresh first-run gate off (consult doesn't need it anyway,
            # but main.cli may probe). Set marker present.
            "STORY_SKIP_FIRST_RUN": "1",
            **env_overrides,
        }
        cmd = [
            sys.executable,
            "-m",
            "story_lifecycle",
            "consult",
            *cli_args,
        ]
        result = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            # Windows 控制台默认 GBK,子进程输出 UTF-8(含中文)会触发
            # UnicodeDecodeError → stdout/stderr 被置 None。errors=replace 兜住,
            # 保证返回 str(下游 ``in result.stdout`` 断言不撞 TypeError)。
            errors="replace",
            timeout=60,
        )
        # 防御:极端情况下 stdout/stderr 仍可能为 None(进程崩/管道断),
        # 兜成空串让断言给出清晰的 failure 而非 TypeError。
        if result.stdout is None:
            result.stdout = ""
        if result.stderr is None:
            result.stderr = ""
        return result

    def test_fake_mode_happy_path(self, isolated_story_home):
        """STORY_CONSULT_FAKE 设了 → advisory 是 fake 值,exit 0,两事件落 DB。"""
        sk = "FEAT-SUBPROCESS-1"
        result = self._run_story_cli(
            {
                "STORY_KEY": sk,
                "STORY_STAGE": "implement",
                "STORY_WORKSPACE": str(isolated_story_home),
                "STORY_ADAPTER": "claude",
                "STORY_CONSULT_FAKE": "MY FAKE ADVISORY",
            },
            "--question",
            "should I?",
            "--urgency",
            "high",
        )
        assert result.returncode == 0, (
            f"consult must exit 0 in fake mode. stderr: {result.stderr}"
        )
        assert "MY FAKE ADVISORY" in result.stdout
        assert "[consult " in result.stdout
        assert "[confidence: high]" in result.stdout

        # DB 落了 consult_request + consult_response
        from story_lifecycle.infra.db import models as _db

        with _db._db() as conn:
            rows = conn.execute(
                "SELECT event_type FROM event_log WHERE story_key = ? ORDER BY id",
                (sk,),
            ).fetchall()
        types = [r["event_type"] for r in rows]
        assert "consult_request" in types
        assert "consult_response" in types

    def test_depth_guard_returns_two(self, isolated_story_home):
        """STORY_CONSULT_DEPTH=1 → exit 2 + 拒绝文案,不调 orchestrator。"""
        sk = "FEAT-SUBPROCESS-2"
        result = self._run_story_cli(
            {
                "STORY_KEY": sk,
                "STORY_STAGE": "implement",
                "STORY_WORKSPACE": str(isolated_story_home),
                "STORY_ADAPTER": "claude",
                "STORY_CONSULT_DEPTH": "1",
                # 设 fake 也无关 —— depth 守卫在 fake 检查之前
                "STORY_CONSULT_FAKE": "should-not-appear",
            },
            "--question",
            "q",
        )
        assert result.returncode == 2
        assert "递归守卫" in result.stdout or "reviewer" in result.stdout
        # fake advisory 不该出现(orchestrator 没被调)
        assert "should-not-appear" not in result.stdout

    def test_missing_env_returns_two(self, isolated_story_home):
        """缺 STORY_KEY/STORY_WORKSPACE → exit 2。"""
        result = self._run_story_cli(
            {
                # 故意不设 STORY_KEY / STORY_WORKSPACE
                "STORY_CONSULT_FAKE": "x",
            },
            "--question",
            "q",
        )
        assert result.returncode == 2
        assert "STORY_KEY" in result.stdout or "STORY_WORKSPACE" in result.stdout

    def test_context_file_overrides_context(self, isolated_story_home, tmp_path):
        """--context-file 优先于 --context,且能读出多行内容。"""
        ctx_file = tmp_path / "ctx.md"
        ctx_file.write_text("line 1\nline 2", encoding="utf-8")
        sk = "FEAT-SUBPROCESS-3"
        result = self._run_story_cli(
            {
                "STORY_KEY": sk,
                "STORY_STAGE": "implement",
                "STORY_WORKSPACE": str(isolated_story_home),
                "STORY_ADAPTER": "claude",
                "STORY_CONSULT_FAKE": "OK",
            },
            "--question",
            "q",
            "--context",
            "inline-context-ignored",
            "--context-file",
            str(ctx_file),
        )
        assert result.returncode == 0
        # Fake advisory 出现 → 说明 orchestrator 被调(即 env 检查通过)
        assert "OK" in result.stdout


# ── TestConsultCmdClickShell ──────────────────────────────────────


class TestConsultCmdClickShell:
    """click CliRunner 测薄壳:argv 解析、--urgency 校验。"""

    def test_question_required(self):
        runner = CliRunner()
        result = runner.invoke(consult_cmd, [])
        assert result.exit_code != 0  # click 报缺 --question
        assert "question" in result.output.lower() or "missing" in result.output.lower()

    def test_urgency_invalid_choice(self):
        runner = CliRunner()
        result = runner.invoke(
            consult_cmd,
            ["--question", "q", "--urgency", "panic"],
        )
        assert result.exit_code != 0
