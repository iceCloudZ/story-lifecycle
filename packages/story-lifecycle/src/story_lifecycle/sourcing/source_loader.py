"""Source profile loader — 按 source_type 解析生命周期模型。

SOURCE-DRIVEN-MODEL: story_states(业务状态机)和 state_map(外部状态→lifecycle_state
映射)原先绑死在 profile(minimal.yaml),但它们驱动的是 source 级行为。迁到 source 维度后,
profile 只管「阶段怎么执行」,source 管「状态拓扑从哪来」。

加载优先级(对称 profile_loader._load_raw):
    项目 .story/source_profiles/ → STORY_HOME/source_profiles/ → 包内置
source_type 为 None/空/未注册 → 回落 default.yaml(通用四状态机)。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# STORY_HOME migrated here when legacy nodes/state.py was removed (ISS-005).
# Mirrors the constant defined in profile_loader.py.
STORY_HOME = Path.home() / ".story-lifecycle"

log = logging.getLogger(__name__)

# 已注册的 source_type 取值收口(替掉散落的 "tapd"/"github" 字面量)。
# 本轮只定义常量,不强制改所有调用点(渐进迁移)。
SOURCE_TYPES = {"tapd", "github", "manual", "swebench"}

# source_type 为 None/空/未注册时回落到此 profile。
DEFAULT_SOURCE_PROFILE = "default"


@dataclass
class SourceProfile:
    """按 source_type 解析的生命周期模型(只读)。

    与 ResolvedProfile 正交:ResolvedProfile 管「阶段怎么执行」(stages/CLI/quality),
    SourceProfile 管「业务状态拓扑」(story_states)和「外部状态映射」(state_map)。
    """

    source_type: str
    # Story 业务状态机(开发/测试/上线...)。每个状态含 stages(本状态要跑的阶段,
    # 引用 profile.stages 名)、next(下一状态)、confirm(转移闸类型)。无 → 空 dict,
    # driver 退化成扁平阶段行为(向后兼容)。
    story_states: dict = field(default_factory=dict)
    # 外部状态 → lifecycle_state 映射(sync 同步时自动写 lifecycle_state)。
    # 泛化自 tapd_state_map,key = item subtype(story/bug/subtask/issue/pr...),
    # value = {external_status: lifecycle_state}。
    state_map: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)


def resolve_source_profile(source_type: str | None) -> SourceProfile:
    """加载并解析一个 source profile。

    source_type 为 None/空/未注册 → 回落 default.yaml。
    找不到对应 yaml 也回落 default(容错,不让查询方崩)。
    """
    name = _normalize_source_type(source_type)
    try:
        raw = _load_raw(name)
    except FileNotFoundError:
        # 回落 default(比如某个 source 还没建 profile 文件)
        if name != DEFAULT_SOURCE_PROFILE:
            log.debug(
                "source profile %s not found, falling back to %s",
                name,
                DEFAULT_SOURCE_PROFILE,
            )
            try:
                raw = _load_raw(DEFAULT_SOURCE_PROFILE)
                name = DEFAULT_SOURCE_PROFILE
            except FileNotFoundError:
                return SourceProfile(source_type=name or DEFAULT_SOURCE_PROFILE)
        else:
            return SourceProfile(source_type=DEFAULT_SOURCE_PROFILE)
    return SourceProfile(
        source_type=name,
        story_states=raw.get("story_states", {}) or {},
        state_map=raw.get("state_map", raw.get("tapd_state_map", {})) or {},
        raw=raw,
    )


def _normalize_source_type(source_type: str | None) -> str:
    """None/空/未注册 → 'default';其余原样返回(按文件名查)。"""
    if not source_type:
        return DEFAULT_SOURCE_PROFILE
    return source_type


def _load_raw(name: str) -> dict:
    """Internal: load raw YAML dict for a source profile.

    搜索顺序:项目 .story/source_profiles/ → STORY_HOME/source_profiles/ → 包内置。
    对称 profile_loader._load_raw。
    """
    import importlib.resources as _ir

    for base in [
        Path.cwd() / ".story",
        STORY_HOME,
    ]:
        path = base / "source_profiles" / f"{name}.yaml"
        if path.exists():
            return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    # 包内置 via importlib.resources
    try:
        ref = _ir.files("story_lifecycle.sourcing.source_profiles").joinpath(
            f"{name}.yaml"
        )
        return yaml.safe_load(ref.read_text(encoding="utf-8")) or {}
    except (FileNotFoundError, TypeError):
        raise FileNotFoundError(f"Source profile not found: {name}")
