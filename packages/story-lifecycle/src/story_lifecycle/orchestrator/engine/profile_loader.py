import dataclasses
import yaml
from dataclasses import dataclass, field
from pathlib import Path

# STORY_HOME migrated here when legacy nodes/state.py was removed (ISS-005).
# Mirrors the constant defined in nodes/__init__.py and planner.py.
STORY_HOME = Path.home() / ".story-lifecycle"


@dataclass
class StageConfig:
    """Single stage's fully resolved configuration.

    Merges profile-level defaults with stage-level overrides at parse time.
    """

    order: int = 0
    description: str = ""
    cli: str = "claude"
    model: str = ""
    provider: str = ""
    execution_mode: str = "interactive_pty"
    skill: str = ""
    confirm: bool = False
    review: bool = True
    max_retries: int = 3
    allowed_providers: list[str] = field(default_factory=list)
    expected_outputs: list[str] = field(default_factory=list)
    next_default: list[str] = field(default_factory=list)
    # Preserve any extra keys from YAML
    _extra: dict = field(default_factory=dict)

    def get(self, key: str, default=None):
        """Dict-like access for backward compatibility."""
        if hasattr(self, key):
            return getattr(self, key)
        return self._extra.get(key, default)


@dataclass
class ResolvedProfile:
    """Fully resolved profile — parsed once at story start, read-only after.

    Replaces repeated load_profile() + get_stage_config() calls throughout nodes.
    """

    name: str
    cli: str = "claude"
    model: str = ""
    provider: str = ""
    execution_mode: str = "interactive_pty"
    stages: dict[str, StageConfig] = field(default_factory=dict)
    quality: dict = field(default_factory=dict)
    adversarial: dict = field(default_factory=dict)
    reviewers: dict = field(default_factory=dict)
    # STORY-STATE-MODEL: Story 业务状态机定义(开发/测试/上线...)。每个状态含
    # stages(本状态要跑的阶段)、next(下一状态)、confirm(转移闸类型)。无该段 → 空
    # dict,driver 退化成扁平阶段行为(向后兼容 realtest/swebench 等 profile)。
    story_states: dict = field(default_factory=dict)
    # TAPD 状态 → lifecycle_state 映射(sync_service 同步时自动写 lifecycle_state)。
    # key = tapd_type(story/bug/subtask),value = {tapd_status: lifecycle_state}。
    tapd_state_map: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    def stage(self, stage_name: str) -> StageConfig:
        """Get resolved stage config, returning empty StageConfig if not found."""
        return self.stages.get(stage_name, StageConfig())

    def to_dict(self) -> dict:
        """Serialize for storage in StoryState."""
        import dataclasses

        stages = {}
        for k, v in self.stages.items():
            stages[k] = dataclasses.asdict(v)
        return {
            "name": self.name,
            "cli": self.cli,
            "model": self.model,
            "provider": self.provider,
            "execution_mode": self.execution_mode,
            "stages": stages,
            "quality": self.quality,
            "adversarial": self.adversarial,
            "reviewers": self.reviewers,
            "story_states": self.story_states,
            "tapd_state_map": self.tapd_state_map,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ResolvedProfile":
        """Deserialize from StoryState storage."""
        stages = {}
        for k, v in data.get("stages", {}).items():
            if isinstance(v, dict):
                extra = {
                    kk: vv
                    for kk, vv in v.items()
                    if kk.startswith("_")
                    or kk not in {f.name for f in dataclasses.fields(StageConfig)}
                }
                v.pop("_extra", None)
                stages[k] = StageConfig(
                    **{
                        kk: vv
                        for kk, vv in v.items()
                        if kk in {f.name for f in dataclasses.fields(StageConfig)}
                    },
                    _extra=extra,
                )
        return cls(
            name=data.get("name", ""),
            cli=data.get("cli", "claude"),
            model=data.get("model", ""),
            provider=data.get("provider", ""),
            execution_mode=data.get("execution_mode", "interactive_pty"),
            stages=stages,
            quality=data.get("quality", {}),
            adversarial=data.get("adversarial", {}),
            reviewers=data.get("reviewers", {}),
            story_states=data.get("story_states", {}),
            tapd_state_map=data.get("tapd_state_map", {}),
        )


def resolve_profile(profile_name: str) -> ResolvedProfile:
    """Load and resolve a profile into a ResolvedProfile object.

    Merges profile-level defaults with stage-level overrides so that
    each StageConfig contains the final, fully-resolved configuration.
    """
    raw = _load_raw(profile_name)
    top_cli = raw.get("cli", "claude")
    top_model = raw.get("model", "")
    top_provider = raw.get("provider", "")
    top_execution_mode = raw.get("execution_mode", "interactive_pty")

    stages = {}
    for stage_name, stage_raw in raw.get("stages", {}).items():
        stages[stage_name] = StageConfig(
            order=stage_raw.get("order", 0),
            description=stage_raw.get("description", ""),
            cli=stage_raw.get("cli", top_cli),
            model=stage_raw.get("model", top_model),
            provider=stage_raw.get("provider", top_provider),
            execution_mode=stage_raw.get("execution_mode", top_execution_mode),
            skill=stage_raw.get("skill", ""),
            confirm=stage_raw.get("confirm", False),
            review=stage_raw.get("review", True),
            max_retries=stage_raw.get("max_retries", 3),
            allowed_providers=stage_raw.get("allowed_providers", []),
            expected_outputs=stage_raw.get("expected_outputs", []),
            next_default=stage_raw.get("next_default", []),
            _extra={
                k: v
                for k, v in stage_raw.items()
                if k
                not in {
                    "order",
                    "description",
                    "cli",
                    "model",
                    "provider",
                    "execution_mode",
                    "skill",
                    "confirm",
                    "review",
                    "max_retries",
                    "allowed_providers",
                    "expected_outputs",
                    "next_default",
                }
            },
        )

    return ResolvedProfile(
        name=profile_name,
        cli=top_cli,
        model=top_model,
        provider=top_provider,
        execution_mode=top_execution_mode,
        stages=stages,
        quality=raw.get("quality", {}),
        adversarial=raw.get("adversarial", {}),
        reviewers=raw.get("reviewers", {}),
        story_states=raw.get("story_states", {}),
        tapd_state_map=raw.get("tapd_state_map", {}),
        raw=raw,
    )


# ---- Legacy API (kept for CLI and external callers) ----


def load_profile(profile_name: str) -> dict:
    """Load a profile YAML. Searches: project .story/ → STORY_HOME → package built-in."""
    return _load_raw(profile_name)


def list_profiles() -> list[dict]:
    """List all available profiles from all search paths.

    Returns ``[{"name", "description", "stages", "execution_mode"}]``.
    Deduplicated by name (project .story/ overrides STORY_HOME overrides built-in).
    """
    import importlib.resources as _ir
    import os as _os
    from pathlib import Path as _Path

    _STORY_HOME = _Path(
        _os.environ.get("STORY_HOME", str(_Path.home() / ".story-lifecycle"))
    )

    seen: dict[str, dict] = {}

    def _scan_dir(base) -> None:
        profiles_dir = base / "profiles"
        if not profiles_dir.exists():
            return
        for f in sorted(profiles_dir.glob("*.yaml")):
            name = f.stem
            if name in seen:
                continue  # higher-priority source already found
            try:
                data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
                if data.get("hidden"):
                    continue  # hidden profiles not shown in picker
                stages = list((data.get("stages") or {}).keys())
                seen[name] = {
                    "name": name,
                    "description": _extract_description(data),
                    "stages": stages,
                    "execution_mode": data.get("execution_mode", "interactive_pty"),
                }
            except Exception:
                continue

    # Priority order: project .story/ > STORY_HOME > package built-in
    _scan_dir(Path.cwd() / ".story")
    _scan_dir(_STORY_HOME)

    # Package built-in
    try:
        pkg_dir = _ir.files("story_lifecycle.entry.profiles")
        for ref in pkg_dir.iterdir():
            name = ref.name
            if not name.endswith(".yaml"):
                continue
            stem = name[:-5]
            if stem in seen:
                continue
            try:
                data = yaml.safe_load(ref.read_text(encoding="utf-8")) or {}
                if data.get("hidden"):
                    continue  # hidden profiles not shown in picker
                stages = list((data.get("stages") or {}).keys())
                seen[stem] = {
                    "name": stem,
                    "description": _extract_description(data),
                    "stages": stages,
                    "execution_mode": data.get("execution_mode", "interactive_pty"),
                }
            except Exception:
                continue
    except Exception:
        pass

    return list(seen.values())


def _extract_description(data: dict) -> str:
    """从 profile yaml 提取一句话描述。

    优先读 ``label`` 字段(人写的简述);没有则 fallback 到阶段拼接。
    """
    label = data.get("label")
    if label:
        return str(label)
    stages = data.get("stages") or {}
    if stages:
        stage_names = list(stages.keys())
        return f"阶段: {' → '.join(stage_names)}"
    return ""


def get_stage_config(profile_name: str, stage_name: str) -> dict:
    profile = _load_raw(profile_name)
    stages = profile.get("stages", {})
    return stages.get(stage_name, {})


def _load_raw(profile_name: str) -> dict:
    """Internal: load raw YAML dict for a profile."""
    import importlib.resources as _ir

    for base in [
        Path.cwd() / ".story",
        STORY_HOME,
    ]:
        path = base / "profiles" / f"{profile_name}.yaml"
        if path.exists():
            return yaml.safe_load(path.read_text(encoding="utf-8"))
    # Package built-in via importlib.resources
    try:
        ref = _ir.files("story_lifecycle.entry.profiles").joinpath(
            f"{profile_name}.yaml"
        )
        return yaml.safe_load(ref.read_text(encoding="utf-8"))
    except (FileNotFoundError, TypeError):
        pass
    raise FileNotFoundError(f"Profile not found: {profile_name}")
