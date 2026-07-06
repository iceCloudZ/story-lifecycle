"""Tests for supervisor Decider — LLM-driven response to code-agent questions.

Supervisor 监督交互式 code agent (claude/codex/kimi),当 agent 提问/要选择时,
``decide_response`` 用注入的 LLM 决策返回 {choice, reason}。纯 Decider,零副作用。
两轨(Claude stream-json / codex-kimi PTY)共用此决策大脑。
"""

import asyncio

import pytest

from story_lifecycle.orchestrator.engine.supervisor import (
    decide_response,
    emit_clarification_request,
    handle_pty_output,
    log_decision,
    supervise_pty_session,
)


class TestDecideResponse:
    def test_parses_llm_json_into_choice_and_reason(self):
        """LLM 返回 JSON 字符串 → decide_response 解析成 {choice, reason}。"""

        def fake_llm(prompt: str) -> str:
            return '{"choice": "option_a", "reason": "blast radius smaller"}'

        result = decide_response(
            question="用方案 A 还是 B?",
            options=["option_a", "option_b"],
            story_facts={"story_key": "FEAT-1", "stage": "implement"},
            llm_invoke=fake_llm,
        )

        assert result == {"choice": "option_a", "reason": "blast radius smaller"}

    def test_strips_markdown_code_fence_around_json(self):
        """LLM 常把 JSON 包在 ```json ... ``` 里,decide_response 要剥离再解析。"""

        def fake_llm(prompt: str) -> str:
            return '```json\n{"choice": "option_b", "reason": "faster"}\n```'

        result = decide_response(
            question="q",
            options=["option_a", "option_b"],
            story_facts={"story_key": "S-2", "stage": "implement"},
            llm_invoke=fake_llm,
        )

        assert result == {"choice": "option_b", "reason": "faster"}

    def test_raises_when_choice_not_in_options(self):
        """LLM 返回的 choice 不在 options → raise(Decider 不替 LLM 圆场,Handler 接)。"""

        def fake_llm(prompt: str) -> str:
            return '{"choice": "option_c", "reason": "unsupported"}'

        with pytest.raises(ValueError):
            decide_response(
                question="q",
                options=["option_a", "option_b"],
                story_facts={"story_key": "S-3", "stage": "implement"},
                llm_invoke=fake_llm,
            )


class TestLogDecision:
    def test_logs_supervisor_decision_event_with_payload(self):
        """log_decision 把决策落 log_event(event_type=supervisor_decision)。"""
        recorded = []

        def fake_log_event(story_key, *, stage, event_type, payload):
            recorded.append(
                {
                    "story_key": story_key,
                    "stage": stage,
                    "event_type": event_type,
                    "payload": payload,
                }
            )

        log_decision(
            story_key="FEAT-1",
            stage="implement",
            adapter="claude",
            question="用 A 还是 B?",
            options=["option_a", "option_b"],
            decision={"choice": "option_a", "reason": "safer"},
            log_event_fn=fake_log_event,
        )

        assert len(recorded) == 1
        entry = recorded[0]
        assert entry["story_key"] == "FEAT-1"
        assert entry["stage"] == "implement"
        assert entry["event_type"] == "supervisor_decision"
        assert entry["payload"]["adapter"] == "claude"
        assert entry["payload"]["choice"] == "option_a"
        assert entry["payload"]["reason"] == "safer"


class TestHandlePtyOutput:
    def test_answers_and_logs_on_awaiting_hit(self):
        """is_awaiting 命中 → 决策 + pty.write 应答 + log。"""
        from types import SimpleNamespace

        writes: list[bytes] = []
        fake_pty = SimpleNamespace(write=lambda d: writes.append(d))
        logs: list[dict] = []

        def fake_log(story_key, *, stage, event_type, payload):
            logs.append({"story_key": story_key, "event_type": event_type, "payload": payload})

        def fake_awaiting(buffer):
            if "A 还是 B" in buffer:
                return ("用 A 还是 B?", ["option_a", "option_b"])
            return None

        def fake_llm(prompt):
            return '{"choice": "option_a", "reason": "safer"}'

        answered = handle_pty_output(
            buffer="提问: 用 A 还是 B?",
            pty=fake_pty,
            adapter="codex",
            story_facts={"story_key": "S-1", "stage": "implement"},
            is_awaiting_fn=fake_awaiting,
            llm_invoke=fake_llm,
            log_event_fn=fake_log,
        )

        assert answered is True
        assert writes == [b"option_a\r"]
        assert len(logs) == 1
        assert logs[0]["event_type"] == "supervisor_decision"
        assert logs[0]["payload"]["choice"] == "option_a"

    def test_no_answer_no_llm_no_log_on_miss(self):
        """is_awaiting 未命中 → 短路:不调 LLM、不写 PTY、不 log(省 token)。

        回归守护:miss 路径必须保持零 LLM 调用(每次决策最多 1 次 LLM,§2.2 原则 7),
        否则 supervisor 会因每条 PTY 输出都打 LLM 而烧 token。
        """
        from types import SimpleNamespace

        writes: list[bytes] = []
        fake_pty = SimpleNamespace(write=lambda d: writes.append(d))
        logs: list[dict] = []
        llm_calls = {"n": 0}

        def fake_log(story_key, *, stage, event_type, payload):
            logs.append({"event_type": event_type})

        def fake_awaiting(buffer):
            return None  # 普通输出,未在等人

        def fake_llm(prompt):
            llm_calls["n"] += 1
            return '{"choice": "x", "reason": "must not be called on miss"}'

        answered = handle_pty_output(
            buffer="正在编辑 src/app.py ... 文件已保存",
            pty=fake_pty,
            adapter="codex",
            story_facts={"story_key": "S-1", "stage": "implement"},
            is_awaiting_fn=fake_awaiting,
            llm_invoke=fake_llm,
            log_event_fn=fake_log,
        )

        assert answered is False
        assert writes == []
        assert logs == []
        assert llm_calls["n"] == 0


class TestSupervisePtySession:
    @pytest.mark.asyncio
    async def test_consumes_tap_answers_awaiting_and_logs(self):
        """supervise_pty_session 持续消费 add_tap 队列:命中提问→应答+log。

        fake pty 预填 chunks 到 tap queue,末尾 None 作哨兵退出循环;断言:
        - add_tap 调一次
        - 命中点被应答(pty.write 收到 choice+'\r')
        - supervisor_decision 事件落 log
        - finally 调 remove_tap(不泄漏 tap)
        """
        from types import SimpleNamespace

        tap = asyncio.Queue()
        tap.put_nowait("正在分析代码...\n".encode("utf-8"))
        tap.put_nowait("请选择方案: A) foo B) bar\n".encode("utf-8"))
        tap.put_nowait(None)  # sentinel → 退出循环

        writes: list[bytes] = []
        logs: list[dict] = []
        add_calls: list[int] = []
        removed: list = []

        def add_tap(maxsize=512):
            add_calls.append(maxsize)
            return tap

        fake_pty = SimpleNamespace(
            add_tap=add_tap,
            remove_tap=lambda t: removed.append(t),
            write=lambda d: writes.append(d),
            alive=True,
        )

        def fake_log(story_key, *, stage, event_type, payload):
            logs.append({"event_type": event_type, "payload": payload})

        def fake_awaiting(buffer):
            if "请选择方案" in buffer:
                return ("请选择方案: A) foo B) bar", ["A", "B"])
            return None

        def fake_llm(prompt):
            return '{"choice": "A", "reason": "foo is simpler"}'

        await supervise_pty_session(
            pty=fake_pty,
            adapter="codex",
            story_facts={"story_key": "S-1", "stage": "implement"},
            is_awaiting_fn=fake_awaiting,
            llm_invoke=fake_llm,
            log_event_fn=fake_log,
        )

        assert add_calls == [512]          # tap 注册一次
        assert writes == [b"A\r"]           # 命中 → 应答一次
        assert len(logs) == 1
        assert logs[0]["event_type"] == "supervisor_decision"
        assert logs[0]["payload"]["choice"] == "A"
        assert removed == [tap]             # finally 清理 tap

    @pytest.mark.asyncio
    async def test_exits_when_pty_dead_without_sentinel(self):
        """pty.alive=False 时,即使 tap 无数据也无 sentinel,循环也能退出(不卡死)。

        真实 ManagedPty 进程死时不会往 tap 推 None(只有 _read_loop 退出),
        所以循环必须靠 pty.alive 轮询退出,否则 task 永久泄漏。
        """
        from types import SimpleNamespace

        # 空 tap + 立即 dead → 靠 alive 轮询退出
        tap = asyncio.Queue()
        fake_pty = SimpleNamespace(
            add_tap=lambda maxsize=512: tap,
            remove_tap=lambda t: None,
            write=lambda d: None,
            alive=False,
        )

        # 不应调任何决策侧
        def boom_awaiting(buffer):
            raise AssertionError("is_awaiting 不应在 dead pty 上被调")

        def boom_llm(prompt):
            raise AssertionError("llm 不应在 dead pty 上被调")

        # 应在轮询周期内返回,不卡死
        await asyncio.wait_for(
            supervise_pty_session(
                pty=fake_pty,
                adapter="codex",
                story_facts={"story_key": "S-2", "stage": "implement"},
                is_awaiting_fn=boom_awaiting,
                llm_invoke=boom_llm,
                log_event_fn=lambda *a, **k: None,
            ),
            timeout=5,
        )
        # 到这里就是通过(dead pty 没卡死,没调 LLM)


# ---------------------------------------------------------------------------
# HITL:design 阶段「claude 逐问 + 人答」——decide_response 不自动答,
# 改返回「暂停 + 推 clarification_request」给人答。详见 docs/design-hitl-runbook.md。
# ---------------------------------------------------------------------------


class TestDecideResponseHitl:
    def test_hitl_returns_pause_action_without_calling_llm(self):
        """HITL 模式:检测到提问 → 不调 llm_invoke,返回 {pause, clarification_request}。

        runbook §5 核心:把 design 从「claude 一次性自动答」改为「暂停等人答」。
        纯 Decider——LLM 不调(人答),DB/SSE/回注归 Handler。
        """
        llm_calls = {"n": 0}

        def fake_llm(prompt):
            llm_calls["n"] += 1
            return '{"choice": "x", "reason": "must not be called in HITL"}'

        result = decide_response(
            question="配置存 hc_user 还是 hc_config?",
            options=["hc_user", "hc_config"],
            story_facts={"story_key": "S-1", "stage": "design"},
            llm_invoke=fake_llm,
            hitl=True,
        )

        assert llm_calls["n"] == 0  # HITL 绝不调 LLM
        assert result["mode"] == "hitl"
        assert result["pause"] is True
        cr = result["clarification_request"]
        assert cr["question"] == "配置存 hc_user 还是 hc_config?"
        assert cr["options"] == ["hc_user", "hc_config"]
        assert cr["ai_suggestion"] is None  # HITL 不预填(人答为主)

    def test_hitl_off_defaults_to_auto_decide(self):
        """hitl 默认 False → 旧行为不变(自动答),回归守护。"""
        result = decide_response(
            question="A 还是 B?",
            options=["option_a", "option_b"],
            story_facts={"story_key": "S-2", "stage": "implement"},
            llm_invoke=lambda p: '{"choice": "option_a", "reason": "r"}',
        )
        assert result == {"choice": "option_a", "reason": "r"}


class TestEmitClarificationRequest:
    def test_logs_clarification_request_event_with_id_and_returns_id(self):
        """emit_clarification_request:生成 id + 落 clarification_request 事件 + 返回 id。

        Handler(非纯 Decider):id 生成 + log_event I/O 在此;Decider 不碰这些。
        返回的 id 供回注端 POST /clarify/answer {id, answer} 引用本轮提问。
        """
        recorded = []

        def fake_log(story_key, *, stage, event_type, payload):
            recorded.append(
                {
                    "story_key": story_key,
                    "stage": stage,
                    "event_type": event_type,
                    "payload": payload,
                }
            )

        rid = emit_clarification_request(
            story_key="S-1",
            stage="design",
            question="配置存哪?",
            options=["hc_user", "hc_config"],
            header="存储位置",
            ai_suggestion=None,
            log_event_fn=fake_log,
            id_factory=lambda: "rid-abc123",
        )

        assert rid == "rid-abc123"
        assert len(recorded) == 1
        entry = recorded[0]
        assert entry["story_key"] == "S-1"
        assert entry["stage"] == "design"
        assert entry["event_type"] == "clarification_request"
        p = entry["payload"]
        assert p["id"] == "rid-abc123"
        assert p["header"] == "存储位置"
        assert p["question"] == "配置存哪?"
        assert p["options"] == ["hc_user", "hc_config"]
        assert p["ai_suggestion"] is None

    def test_header_defaults_to_question_when_blank(self):
        """header 缺省取 question 文本(前端展示主标题)。"""
        recorded = []

        def fake_log(story_key, *, stage, event_type, payload):
            recorded.append(payload)

        emit_clarification_request(
            story_key="S-1",
            stage="design",
            question="用方案 A 还是 B?",
            options=["A", "B"],
            log_event_fn=fake_log,
            id_factory=lambda: "x",
        )
        assert recorded[0]["header"] == "用方案 A 还是 B?"
