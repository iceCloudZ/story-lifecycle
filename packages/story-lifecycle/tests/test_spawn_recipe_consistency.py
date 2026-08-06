"""spawn 双路径一致性测试（设计 14 §6.3）— api 与 executors 走同一 spawn 配方。

设计 14 (D3) 把两条 spawn 路径（api._spawn_story_agent_pty 交互式 /
executors.InteractiveStageExecutor.spawn 编排线程）收敛到
infra/terminal/spawn_recipe.spawn_agent_pty。本文件锁定：
- 两条路径都调 spawn_agent_pty（不再各自实现 resume/NEW 判定）
- marker 文件格式一致（session_id/name/stage 三字段）
- resume/NEW 判据一致（DB 捕获 sid 优先 + prespecified marker 兜底）
"""

import json

import pytest

import story_lifecycle.infra.terminal.spawn_recipe as recipe
import story_lifecycle.orchestrator.service.api as api
from story_lifecycle.infra.db import models as db
from story_lifecycle.orchestrator.executors import InteractiveStageExecutor


class _FakeAdapter:
    """ShellAdapter 替身:记录 start_session 入参;不接 sid 捕获。"""

    name = "kimi"
    prespecified_session_id = False

    def __init__(self):
        self.calls = []

    def start_session(
        self, model, prompt="", session_id="", session_name="", resume=False
    ):
        self.calls.append(
            {
                "session_id": session_id,
                "resume": resume,
                "prompt": prompt,
                "session_name": session_name,
            }
        )
        from story_lifecycle.knowledge.adapters.base import SessionSpec

        return SessionSpec(command=["kimi"], pty_prompt="", readiness_marker=None)

    def make_sid_capturer(self, *a, **k):
        return None


@pytest.fixture(autouse=True)
def _stub_pty(monkeypatch):
    """stub ensure_agent_pty（真实调用点在 pty 模块）。"""
    import story_lifecycle.infra.terminal.pty as pty_mod

    class _FakePty:
        alive = True
        session_id = "reg-id"

        def kill(self):
            self.alive = False

    monkeypatch.setattr(pty_mod, "ensure_agent_pty", lambda *a, **k: ("reg-id", _FakePty()))


def _seed_story(tmp_path, story_key="SPAWN-1", profile="minimal"):
    ws = tmp_path / "ws"
    ws.mkdir()
    db.create_story(story_key, "spawn 一致性", str(ws), profile=profile)
    db.update_story(
        story_key,
        context_json=json.dumps(
            {
                "_plan_confirmed": True,
                "_agent_actions": [
                    {"action": "launch", "stage": "design", "adapter": "kimi"}
                ],
            },
            ensure_ascii=False,
        ),
    )
    return story_key


class TestBothPathsUseSpawnRecipe:
    def test_api_path_calls_spawn_agent_pty(self, tmp_path, isolated_story_home, monkeypatch):
        """api._spawn_story_agent_pty 走 spawn_agent_pty（不再自己拼 resume）。"""
        key = _seed_story(tmp_path)
        story = db.get_story(key)
        called = []

        def _fake_recipe(*a, **kw):
            called.append(kw)
            return recipe.SpawnResult(
                session_id="reg-id",
                pty=object(),
                is_resume=False,
                session_uuid="uuid5",
                use_sid="uuid5",
                marker=tmp_path / "m.json",
                adapter_name="kimi",
                spawn_cwd=str(tmp_path / "ws"),
            )

        monkeypatch.setattr(recipe, "spawn_agent_pty", _fake_recipe)
        monkeypatch.setattr(api, "_build_stage_launch_prompt", lambda s: "seed")
        # 跳过死后存活检查的 sleep
        import time as _time

        monkeypatch.setattr(_time, "sleep", lambda _s: None)
        adapter = _FakeAdapter()
        api._spawn_story_agent_pty(story, adapter, "sonnet")
        assert len(called) == 1
        assert called[0]["story_key"] == key
        assert called[0]["stage"] == "design"
        assert called[0]["workspace"] == str(tmp_path / "ws")
        assert called[0]["seed"] == "seed"

    def test_executors_path_calls_spawn_agent_pty(self, tmp_path, isolated_story_home, monkeypatch):
        """InteractiveStageExecutor.spawn 走 spawn_agent_pty。"""
        key = _seed_story(tmp_path)
        called = []

        def _fake_recipe(*a, **kw):
            called.append(kw)
            return recipe.SpawnResult(
                session_id="reg-id",
                pty=object(),
                is_resume=False,
                session_uuid="uuid5",
                use_sid="uuid5",
                marker=tmp_path / "m.json",
                adapter_name="kimi",
                spawn_cwd=str(tmp_path / "ws"),
            )

        monkeypatch.setattr(recipe, "spawn_agent_pty", _fake_recipe)
        # get_adapter 在 executors 内部按名解析 —— 直接用真实 kimi 之外的 stub
        import story_lifecycle.knowledge.adapters as adapters_mod

        monkeypatch.setattr(adapters_mod, "get_adapter", lambda n: _FakeAdapter())
        executor = InteractiveStageExecutor()
        executor.spawn(key, "design", {"action": "launch", "stage": "design", "adapter": "kimi"})
        assert len(called) == 1
        assert called[0]["story_key"] == key
        assert called[0]["stage"] == "design"
        assert called[0]["seed"]  # LaunchSeedBuilder 产物


class TestMarkerFormatConsistent:
    def test_recipe_writes_marker_three_fields(self, tmp_path, isolated_story_home, monkeypatch):
        """spawn_agent_pty 的 marker 格式：session_id/name/stage 三字段。"""
        key = _seed_story(tmp_path)
        import story_lifecycle.infra.terminal.pty as pty_mod

        class _FakePty:
            alive = True
            session_id = "reg-id"

        monkeypatch.setattr(
            pty_mod, "ensure_agent_pty", lambda *a, **k: ("reg-id", _FakePty())
        )
        adapter = _FakeAdapter()
        res = recipe.spawn_agent_pty(
            adapter,
            "sonnet",
            story_key=key,
            stage="design",
            workspace=str(tmp_path / "ws"),
            spawn_cwd=str(tmp_path / "ws"),
            seed="seed",
            env={},
        )
        assert res.marker.exists()
        data = json.loads(res.marker.read_text(encoding="utf-8"))
        assert set(data.keys()) == {"session_id", "name", "stage"}
        assert data["stage"] == "design"
        assert data["name"] == f"{key}-design"
        # session_id = compute_session_id 三字段
        assert data["session_id"] == db.compute_session_id(key, "design", "kimi")

    def test_resume_uses_db_captured_sid(self, tmp_path, isolated_story_home, monkeypatch):
        """DB 有捕获 sid → resume=True 且透传该 sid（不覆盖 marker）。"""
        key = _seed_story(tmp_path)
        db.upsert_session(key, "design", "kimi", session_id="session_captured")
        adapter = _FakeAdapter()
        res = recipe.spawn_agent_pty(
            adapter,
            "sonnet",
            story_key=key,
            stage="design",
            workspace=str(tmp_path / "ws"),
            spawn_cwd=str(tmp_path / "ws"),
            seed="seed",
            env={},
        )
        assert res.is_resume is True
        assert adapter.calls[0]["resume"] is True
        assert adapter.calls[0]["session_id"] == "session_captured"
        assert not res.marker.exists()  # resume 不重写 marker

    def test_no_resume_without_captured_sid(self, tmp_path, isolated_story_home):
        """marker 存在但 DB 无捕获 sid（kimi）→ 不 resume（防 -S <uuid5> 假会话）。"""
        key = _seed_story(tmp_path)
        marker = (
            tmp_path / "ws" / ".story" / "context" / key / "session_design.json"
        )
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("{}", encoding="utf-8")
        adapter = _FakeAdapter()
        res = recipe.spawn_agent_pty(
            adapter,
            "sonnet",
            story_key=key,
            stage="design",
            workspace=str(tmp_path / "ws"),
            spawn_cwd=str(tmp_path / "ws"),
            seed="seed",
            env={},
        )
        assert res.is_resume is False
        assert adapter.calls[0]["resume"] is False
