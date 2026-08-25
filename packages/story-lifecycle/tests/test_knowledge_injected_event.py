"""M3 (B5) — knowledge_injected 注入事件落库单测（DESIGN §4.5）。

事实链：build_knowledge_section 非空 → event_log 落 knowledge_injected
{task_type, chars, degraded}；空 → 不落；DB 失败 → 不抛、注入照常返回。
"""

from story_lifecycle.orchestrator.engine import prompt_sections


def _patch(monkeypatch, story_ctx_json: str | None, events: list):
    monkeypatch.setattr(
        prompt_sections.context_providers,
        "get_knowledge_context",
        lambda sk, ws, st: "## 知识\nplaybook 内容",
    )
    monkeypatch.setattr(
        "story_lifecycle.infra.db.models.get_story",
        lambda sk: ({"context_json": story_ctx_json} if story_ctx_json is not None else None),
    )
    monkeypatch.setattr(
        "story_lifecycle.infra.db.events.log_event",
        lambda sk, st, et, payload=None: events.append((sk, st, et, payload)),
    )


def test_injection_with_task_type_logs_event(monkeypatch):
    events: list = []
    _patch(monkeypatch, '{"task_type": "credit-limit"}', events)

    out = prompt_sections.build_knowledge_section("tapd-1", "D:/ws", "design")

    assert "## 知识" in out
    assert len(events) == 1
    sk, st, et, payload = events[0]
    assert (sk, st, et) == ("tapd-1", "design", "knowledge_injected")
    assert payload["task_type"] == "credit-limit"
    assert payload["degraded"] is False
    assert payload["chars"] == len("## 知识\nplaybook 内容")


def test_injection_without_task_type_marks_degraded(monkeypatch):
    """task_type 缺失 = 降级层注入（B1③），degraded=true 供离线区分效果。"""
    events: list = []
    _patch(monkeypatch, "{}", events)

    prompt_sections.build_knowledge_section("tapd-1", "D:/ws", "design")

    assert events[0][3]["task_type"] == "none"
    assert events[0][3]["degraded"] is True


def test_empty_injection_logs_nothing(monkeypatch):
    events: list = []
    monkeypatch.setattr(
        prompt_sections.context_providers,
        "get_knowledge_context",
        lambda sk, ws, st: None,
    )
    monkeypatch.setattr(
        "story_lifecycle.infra.db.models.get_story", lambda sk: None
    )
    monkeypatch.setattr(
        "story_lifecycle.infra.db.events.log_event",
        lambda sk, st, et, payload=None: events.append((sk, st, et, payload)),
    )

    out = prompt_sections.build_knowledge_section("tapd-1", "D:/ws", "design")

    assert out == ""
    assert events == []


def test_db_failure_never_blocks_injection(monkeypatch):
    """事件落库失败 → 注入照常返回（度量绝不阻塞渲染）。"""
    monkeypatch.setattr(
        prompt_sections.context_providers,
        "get_knowledge_context",
        lambda sk, ws, st: "## 知识\nx",
    )

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr("story_lifecycle.infra.db.models.get_story", boom)

    out = prompt_sections.build_knowledge_section("tapd-1", "D:/ws", "design")
    assert "## 知识" in out  # 不抛、内容照常
