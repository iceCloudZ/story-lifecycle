import yaml
from pathlib import Path

from .state import STORY_HOME


def load_profile(profile_name: str) -> dict:
    """Load a profile YAML. Searches: project .story/ → STORY_HOME → package built-in."""
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
        ref = _ir.files("story_lifecycle.profiles").joinpath(f"{profile_name}.yaml")
        return yaml.safe_load(ref.read_text(encoding="utf-8"))
    except (FileNotFoundError, TypeError):
        pass
    raise FileNotFoundError(f"Profile not found: {profile_name}")


def get_stage_config(profile_name: str, stage_name: str) -> dict:
    profile = load_profile(profile_name)
    stages = profile.get("stages", {})
    return stages.get(stage_name, {})
