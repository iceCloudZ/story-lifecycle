"""build_story_spawn_env — 统一 code agent spawn env builder 的单元测试。

回归(2026-07-28):三条 spawn 路径(planner headless / planner PTY / api 交互式
PTY)各自手写 env 字典,全部漏 STORY_TITLE → `story tool declare` 用空 title 算
evidence 子目录,slug 退化成字面量 "需求",与 PRD 的 title-slug 目录不一致;
api 交互式路径甚至整个 env 都没传。抽 build_story_spawn_env 统一修复。
"""

import os

from story_lifecycle.infra.story_paths import (
    ENV_STORY_ADAPTER,
    ENV_STORY_KEY,
    ENV_STORY_STAGE,
    ENV_STORY_TITLE,
    ENV_STORY_WORKSPACE,
    build_story_spawn_env,
)


def _story(**over):
    base = {
        "story_key": "local-amountraise-rerun",
        "workspace": "D:/worktrees/amountraise-rerun",
        "title": "【事件中心】新增提额成功事件(新版本重跑)",
    }
    base.update(over)
    return base


def test_all_five_story_vars_injected():
    """KEY/STAGE/WORKSPACE/ADAPTER/TITLE 五件套都注入(downstream declare/consult/MCP 都依赖)。"""
    env = build_story_spawn_env(_story(), "verify", "claude")
    assert env[ENV_STORY_KEY] == "local-amountraise-rerun"
    assert env[ENV_STORY_STAGE] == "verify"
    assert env[ENV_STORY_WORKSPACE] == "D:/worktrees/amountraise-rerun"
    assert env[ENV_STORY_ADAPTER] == "claude"
    # TITLE 是关键:漏了它 evidence 子目录就退化成 "-需求"。
    assert env[ENV_STORY_TITLE] == "【事件中心】新增提额成功事件(新版本重跑)"


def test_title_passthrough_drives_evidence_subdir_name():
    """STORY_TITLE 透传 → story_short_slug 能算出与 PRD 同源的子目录名。

    这就是原本坏掉的不变量:PRD 落 local-amountraise-rerun-事件中心新增提额成功事件/,
    而 spec/delivery 因 title 空落进 local-amountraise-rerun-需求/。
    """
    from story_lifecycle.infra.story_paths import story_short_slug

    env = build_story_spawn_env(_story(), "verify", "claude")
    slug = story_short_slug(env[ENV_STORY_TITLE])
    assert slug == "事件中心新增提额成功事件"  # 前 12 字符,与 PRD 目录一致


def test_missing_title_falls_back_to_demand():
    """title 缺失时 slug 退回 fallback "需求"(保持旧行为,不崩)。"""
    env = build_story_spawn_env(_story(title=""), "verify", "claude")
    assert env[ENV_STORY_TITLE] == ""


def test_inherits_os_environ_base():
    """builder 以 os.environ 为基底(子进程保留 serve 进程环境:PATH/HOME/lang 等)。"""
    os.environ["STORY_TEST_MARKER"] = "present"
    try:
        env = build_story_spawn_env(_story(), "verify", "claude")
        assert env.get("STORY_TEST_MARKER") == "present"
    finally:
        del os.environ["STORY_TEST_MARKER"]


def test_story_story_key_overrides_inherited_env():
    """注入值覆盖 os.environ 里同名旧值(新 spawn 的 story 上下文优先于父进程残留)。"""
    os.environ["STORY_KEY"] = "STALE_FROM_PARENT"
    os.environ["STORY_TITLE"] = "STALE_TITLE"
    try:
        env = build_story_spawn_env(_story(), "build", "kimi")
        assert env[ENV_STORY_KEY] == "local-amountraise-rerun"
        assert env[ENV_STORY_TITLE] == "【事件中心】新增提额成功事件(新版本重跑)"
    finally:
        del os.environ["STORY_KEY"]
        del os.environ["STORY_TITLE"]


def test_missing_fields_default_to_empty_not_crash():
    """story dict 字段缺失(workspace/title 不在)→ 空串,不崩(spawn 不该因元数据缺失失败)。"""
    env = build_story_spawn_env({"story_key": "K1"}, "design", "")
    assert env[ENV_STORY_KEY] == "K1"
    assert env[ENV_STORY_WORKSPACE] == ""
    assert env[ENV_STORY_TITLE] == ""
    assert env[ENV_STORY_ADAPTER] == ""
