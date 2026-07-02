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
        raw=raw,
    )


# ---- Legacy API (kept for CLI and external callers) ----


def load_profile(profile_name: str) -> dict:
    """Load a profile YAML. Searches: project .story/ → STORY_HOME → package built-in."""
    return _load_raw(profile_name)


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
        ref = _ir.files("story_lifecycle.entry.profiles").joinpath(f"{profile_name}.yaml")
        return yaml.safe_load(ref.read_text(encoding="utf-8"))
    except (FileNotFoundError, TypeError):
        pass
    raise FileNotFoundError(f"Profile not found: {profile_name}")
