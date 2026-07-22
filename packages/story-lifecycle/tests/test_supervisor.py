"""Tests for supervisor Decider — LLM-driven response to code-agent questions.

Supervisor 监督交互式 code agent (claude/codex/kimi),当 agent 提问/要选择时,
``decide_response`` 用注入的 LLM 决策返回 {choice, reason}。纯 Decider,零副作用。
两轨(Claude stream-json / codex-kimi PTY)共用此决策大脑。
"""

import asyncio

import pytest

from story_lifecycle.orchestrator.engine.supervisor import (
    decide_response,
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
    def test_auto_confirm_answers_and_logs_on_awaiting_hit(self):
        """auto_confirm=True(全自动场景):is_awaiting 命中 → 决策 + pty.write 应答 + log。

        仅 profile 显式 auto_confirm=True(benchmark/CI)才走 LLM 自动应答。
        """
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
            story_facts={"story_key": "S-1", "stage": "implement", "auto_confirm": True},
            is_awaiting_fn=fake_awaiting,
            llm_invoke=fake_llm,
            log_event_fn=fake_log,
        )

        assert answered is True
        assert writes == [b"option_a\r"]
        assert len(logs) == 1
        assert logs[0]["event_type"] == "supervisor_decision"
        assert logs[0]["payload"]["choice"] == "option_a"

    def test_manual_mode_does_not_write_pty_or_call_llm(self):
        """回归:默认 auto_confirm 未设/False(人工盯)→ 命中提问也不调 LLM、不写 PTY。

        旧默认是"无条件 LLM 自动答",会把 code-agent 正常提问误应答、烧 token、往 PTY
        塞噪声输入。翻转后:人工模式仅落 awaiting_confirm 事件 + 桌面通知,人工在终端
        自己看到提示自己答。supervisor 零 LLM 调用。
        """
        from types import SimpleNamespace

        writes: list[bytes] = []
        fake_pty = SimpleNamespace(write=lambda d: writes.append(d))
        logs: list[dict] = []
        llm_calls = {"n": 0}

        def fake_log(story_key, *, stage, event_type, payload):
            logs.append({"story_key": story_key, "event_type": event_type, "payload": payload})

        def fake_awaiting(buffer):
            if "A 还是 B" in buffer:
                return ("用 A 还是 B?", ["option_a", "option_b"])
            return None

        def fake_llm(prompt):
            llm_calls["n"] += 1
            return '{"choice": "option_a", "reason": "must not be called in manual mode"}'

        # notify.send 是软依赖(plyer 可能不可用)→ mock 掉避免测试环境弹窗/报错
        import story_lifecycle.orchestrator.engine.supervisor as sup_mod

        orig_notify = getattr(sup_mod, "_notify_awaiting", None)
        sup_mod._notify_awaiting = lambda *a, **k: None
        try:
            answered = handle_pty_output(
                buffer="提问: 用 A 还是 B?",
                pty=fake_pty,
                adapter="codex",
                # auto_confirm 缺省 = False(人工盯)
                story_facts={"story_key": "S-1", "stage": "implement"},
                is_awaiting_fn=fake_awaiting,
                llm_invoke=fake_llm,
                log_event_fn=fake_log,
            )
        finally:
            if orig_notify is not None:
                sup_mod._notify_awaiting = orig_notify

        assert answered is True             # 命中了(清 buffer 防重复触发)
        assert writes == []                 # 但不写 PTY —— 人工自己答
        assert llm_calls["n"] == 0          # 不调 LLM —— 零 token
        assert len(logs) == 1
        assert logs[0]["event_type"] == "awaiting_confirm"  # 落的是"待确认"事件,不是决策
        assert logs[0]["payload"]["question"] == "用 A 还是 B?"
        assert logs[0]["payload"]["options"] == ["option_a", "option_b"]

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
            story_facts={"story_key": "S-1", "stage": "implement", "auto_confirm": True},
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
