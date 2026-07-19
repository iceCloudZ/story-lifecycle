"""tests/e2e conftest: 默认跳过 real_e2e / real_web_e2e，除非 -m 显式选。

这样 `pytest`（默认）不跑真实 AI 测试（慢/贵/需 key + 浏览器）；
`pytest -m real_e2e` / `pytest -m real_web_e2e` 才跑对应那组。
"""
import pytest

# 真实资源消耗型 marker：默认全跳过，需显式 -m 选中。
_OPT_IN_MARKERS = ("real_e2e", "real_web_e2e")


def pytest_collection_modifyitems(config, items):
    markexpr = config.getoption("-m") or ""
    for marker in _OPT_IN_MARKERS:
        if marker in markexpr:
            continue  # 该组被显式选中，不 skip
        skip = pytest.mark.skip(
            reason=(
                f"{marker} 默认跳过；用 `pytest -m {marker}` 跑"
                "（real_e2e 需 claude/codex CLI；real_web_e2e 额外需 WebBridge daemon + 浏览器扩展）"
            )
        )
        for item in items:
            if marker in item.keywords:
                item.add_marker(skip)


# ---------------------------------------------------------------------------
# webbridge_server fixture — self-contained, defined here (not in the testing
# package module) because pytest only discovers fixtures from conftest.py and
# test files, not from arbitrary library modules. It does its OWN DB isolation
# (the autouse _isolated_db lives in packages/story-lifecycle/tests/conftest.py,
# a different conftest subtree this test does not inherit), so real
# ~/.story-lifecycle/story.db is never touched.
# ---------------------------------------------------------------------------


@pytest.fixture
def webbridge_server(tmp_path, monkeypatch):
    """Boot a real uvicorn server against an isolated per-test STORY_HOME.

    Returns a :class:`testing.web.RunningServer` (with ``base_url`` /
    ``ws_base_url``). The server runs in a same-process thread so it shares the
    monkeypatched env: ``get_db_path()`` reads ``STORY_HOME`` live
    (infra/db/models.py:66-71) and resolves the temp DB.
    """
    from testing.web import start_uvicorn_server

    story_home = tmp_path / "story-home"
    story_home.mkdir()
    monkeypatch.setenv("STORY_HOME", str(story_home))

    # init_db against the temp home BEFORE the server's lifespan does, so the
    # first request finds a ready DB; lifespan init_db() is idempotent anyway.
    from story_lifecycle.infra.db import models as db

    monkeypatch.setattr(db, "get_db_path", lambda: story_home / "story.db")
    db.init_db()

    running = start_uvicorn_server(str(story_home))
    try:
        yield running
    finally:
        running.stop()


@pytest.fixture
def real_webbridge_server(tmp_path, monkeypatch):
    """Boot a real uvicorn server against the user's REAL ~/.story-lifecycle DB.

    Unlike ``webbridge_server`` (isolated temp DB), this connects to the actual
    story-lifecycle environment — so registered workspaces (e.g. hc-all), past
    stories, and the real config.yaml are all visible. Required when a scenario
    must go through the SPA's IntakeStartModal, whose workspace dropdown is
    populated from the registered-workspace list (empty in an isolated DB).

    Consequences (the caller accepts these by using this fixture):
      * stories created here land in the real ~/.story-lifecycle/story.db
      * .story/ artifacts land in the real workspace (e.g. D:\\hc-all\\.story)
      * real LLM calls, real Claude CLI, real code changes in the workspace

    The scenario's WorkspacePrep.cleanup still removes injected spec files and
    AI-generated impl files, but the DB story row is NOT auto-deleted (deleting
    a story is a destructive op the user should keep control of).
    """
    import os

    from story_lifecycle.infra.db import models as db
    from story_lifecycle.infra import config as cfg

    # Use the real home (default ~/.story-lifecycle). Ensure the env points at it
    # so the in-process server thread + Claude CLI both resolve the same DB.
    real_home = cfg.CONFIG_DIR  # ~/.story-lifecycle
    monkeypatch.setenv("STORY_HOME", str(real_home))
    db.init_db()  # idempotent; ensures tables exist on first run

    from testing.web import start_uvicorn_server

    running = start_uvicorn_server(str(real_home))
    try:
        yield running
    finally:
        running.stop()
