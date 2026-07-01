"""Configuration IO layer (⑤ infra).

Pure config-file read/merge/write primitives shared by every layer.
Moved here from `cli/setup.py` (ISS-006) so that `sources/`,
`context_providers/`, and `orchestrator/` no longer depend on the entry
layer (`cli/`) just to read config — fixing a layering inversion.

Entry-layer helpers that *interpret* config (wizard, env loading, sub-types)
remain in `cli/setup.py`; they re-import these primitives from here.
"""

from pathlib import Path

import yaml

CONFIG_DIR = Path.home() / ".story-lifecycle"
CONFIG_FILE = CONFIG_DIR / "config.yaml"


def _merge_config(existing: dict, updates: dict) -> dict:
    """Merge updates into existing config. Preserves keys not in updates."""
    merged = dict(existing)
    merged.update(updates)
    return merged


def get_config() -> dict:
    """Load current config, or empty dict."""
    if not CONFIG_FILE.exists():
        return {}
    try:
        return yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}


def save_config(config: dict):
    """Merge `config` into the on-disk config file and persist."""
    existing = get_config()
    merged = _merge_config(existing, config)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(
        yaml.dump(merged, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )
