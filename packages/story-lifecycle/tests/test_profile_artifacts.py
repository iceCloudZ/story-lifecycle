"""1.1 — profile schema 强制契约:每个 stage 至少一个文件类 artifacts。

设计依据:DESIGN-artifact-driven-stage-completion §1.3(基石约束 = schema 强制契约)。
每个 stage 必须声明至少一个文件成果物(文件路径/glob/"git"),否则编排器无机器可查的
完成信号,会回退到不可信的 done.json 自报模型。resolve_profile 在加载时校验。
"""

from __future__ import annotations

import pytest

from story_lifecycle.orchestrator.engine.profile_loader import (
    ProfileValidationError,
    StageConfig,
    resolve_profile,
)


# ---- 所有内置 profile 都应通过校验(每个 stage 都有 artifacts) ----

_BUILTIN_PROFILES = [
    "minimal",
    "realtest",
    "strict",
    "single-pass",
    "demo",
    "headless-smoke",
    "swebench",
]


@pytest.mark.parametrize("name", _BUILTIN_PROFILES)
def test_builtin_profiles_pass_artifact_contract(name):
    """所有内置 profile 加载时都应通过 artifacts 强制契约。"""
    rp = resolve_profile(name)
    assert rp.stages, f"profile {name} 没有 stage"
    for stage_name, cfg in rp.stages.items():
        assert cfg.artifacts, (
            f"profile {name} 的 stage {stage_name} 缺 artifacts"
        )


def test_minimal_artifacts_are_machine_checkable():
    """minimal 的 artifacts 应含三类可机器检查的形式(文件路径 / git / 文件路径)。"""
    rp = resolve_profile("minimal")
    assert rp.stage("design").artifacts == ["story/spec.md"]
    assert rp.stage("build").artifacts == ["git"]
    assert rp.stage("verify").artifacts == ["story/test-report.md"]


# ---- 缺 artifacts 的 profile 必须拒绝加载 ----


def _make_resolved_profile(stages: dict[str, StageConfig]):
    """构造一个 ResolvedProfile,绕开 YAML 加载(直接喂 stages)。"""
    from story_lifecycle.orchestrator.engine.profile_loader import ResolvedProfile

    return ResolvedProfile(name="test", stages=stages)


def test_profile_missing_artifacts_rejected():
    """stage 缺 artifacts → resolve_profile 抛 ProfileValidationError。"""
    from story_lifecycle.orchestrator.engine.profile_loader import _validate_artifacts

    profile = _make_resolved_profile(
        {
            "design": StageConfig(artifacts=[]),  # 缺 artifacts
            "build": StageConfig(artifacts=["git"]),
        }
    )
    with pytest.raises(ProfileValidationError) as exc_info:
        _validate_artifacts(profile)
    msg = str(exc_info.value)
    assert "design" in msg  # 报 offending stage
    assert "build" not in msg  # build 合规,不应出现在 missing 列表


def test_profile_all_stages_missing_artifacts_lists_all():
    """多个 stage 都缺 → 报错一次性列出全部(便于一次修完)。"""
    from story_lifecycle.orchestrator.engine.profile_loader import _validate_artifacts

    profile = _make_resolved_profile(
        {
            "design": StageConfig(artifacts=[]),
            "build": StageConfig(artifacts=[]),
        }
    )
    with pytest.raises(ProfileValidationError) as exc_info:
        _validate_artifacts(profile)
    msg = str(exc_info.value)
    assert "design" in msg
    assert "build" in msg


def test_resolve_profile_from_dict_roundtrip_preserves_artifacts():
    """to_dict/from_dict 往返应保留 artifacts(StoryState 序列化路径)。"""
    rp = resolve_profile("minimal")
    d = rp.to_dict()
    assert d["stages"]["design"]["artifacts"] == ["story/spec.md"]
    # from_dict 应重建出带 artifacts 的 StageConfig
    from story_lifecycle.orchestrator.engine.profile_loader import ResolvedProfile

    rp2 = ResolvedProfile.from_dict(d)
    assert rp2.stage("design").artifacts == ["story/spec.md"]
    assert rp2.stage("build").artifacts == ["git"]
