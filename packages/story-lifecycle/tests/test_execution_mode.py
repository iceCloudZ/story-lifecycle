from story_lifecycle.orchestrator.engine.profile_loader import resolve_profile
from story_lifecycle.infra.db import models as db


def test_minimal_profile_defaults_to_interactive_pty():
    profile = resolve_profile("minimal")

    assert profile.execution_mode == "interactive_pty"
    assert profile.stage("design").execution_mode == "interactive_pty"


def test_swebench_profile_explicitly_uses_headless():
    profile = resolve_profile("swebench")

    assert profile.execution_mode == "headless"
    assert profile.stage("implement").execution_mode == "headless"


def test_done_watcher_selects_only_ready_interactive_story(
    isolated_story_home, tmp_path
):
    from story_lifecycle.orchestrator.engine.graph import find_ready_interactive_stories

    ready_workspace = tmp_path / "ready"
    ready_workspace.mkdir()
    done = ready_workspace / ".story" / "done" / "READY-1"
    done.mkdir(parents=True)
    (done / "design.json").write_text("{}", encoding="utf-8")

    db.upsert_story(
        "READY-1",
        workspace=str(ready_workspace),
        status="active",
    )
    db.update_story(
        "READY-1",
        context_json=(
            '{"_active_execution":{"stage":"design","mode":"interactive_pty"}}'
        ),
    )
    db.upsert_story(
        "PAUSED-1",
        workspace=str(ready_workspace),
        status="paused",
    )
    db.update_story(
        "PAUSED-1",
        context_json=(
            '{"_active_execution":{"stage":"design","mode":"interactive_pty"}}'
        ),
    )

    assert find_ready_interactive_stories() == ["READY-1"]


def test_terminal_spawn_starts_profile_agent_not_shell(
    isolated_story_home, tmp_path, monkeypatch
):
    import story_lifecycle.orchestrator.service.api as api

    db.upsert_story("TERM-1", workspace=str(tmp_path), profile="minimal")
    calls = []

    class FakeAdapter:
        def interactive_launch_cmd(self, model):
            return ["claude-test"]

    monkeypatch.setattr(api, "get_adapter", lambda name: FakeAdapter(), raising=False)
    monkeypatch.setattr(
        api,
        "ensure_agent_pty",
        # ensure_agent_pty returns (session_id, pty); append() returns None,
        # so fall through to a valid tuple to satisfy the caller's unpacking.
        lambda *args, **kwargs: calls.append((args, kwargs)) or ("session-1", object()),
        raising=False,
    )

    result = api.api_spawn_pty("TERM-1")

    assert calls[0][0][1] == ["claude-test"]
    assert result["purpose"] == "agent"
