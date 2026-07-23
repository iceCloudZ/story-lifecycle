"""Configuration IO layer (⑤ infra).

Pure config-file read/merge/write primitives shared by every layer.
Moved here from `cli/setup.py` (ISS-006) so that `sources/`,
`context_providers/`, and `orchestrator/` no longer depend on the entry
layer (`cli/`) just to read config — fixing a layering inversion.

Entry-layer helpers that *interpret* config (wizard, env loading, sub-types)
remain in `cli/setup.py`; they re-import these primitives from here.
"""

import os
from pathlib import Path

import yaml

CONFIG_DIR = Path.home() / ".story-lifecycle"
CONFIG_FILE = CONFIG_DIR / "config.yaml"


def _merge_config(existing: dict, updates: dict) -> dict:
    """Shallow merge (kept for backward compat). Use _merge_config_deep instead."""
    merged = dict(existing)
    merged.update(updates)
    return merged


def _merge_config_deep(base: dict, updates: dict) -> dict:
    """Deep merge: nested dicts merged recursively, leaf values replaced.

    grok-build §6.1: 浅合并(dict.update)会把嵌套 dict 整个替换掉,
    丢失同层级其它键。深合并只替叶节点,保留同层级兄弟。
    """
    result = dict(base)
    for k, v in updates.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _merge_config_deep(result[k], v)
        else:
            result[k] = v
    return result


def get_config() -> dict:
    """Load current config, or empty dict."""
    if not CONFIG_FILE.exists():
        return {}
    try:
        return yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}


def save_config(config: dict):
    """Merge `config` into the on-disk config file and persist atomically.

    grok-build §6.1: 原子写(temp file + fsync + rename)防止写一半崩溃留半个
    文件;深合并防止嵌套 dict 整个替换丢键。config 写得勤(setup 向导、
    每次 autonomy trace),非原子写迟早出半个文件。
    """
    import tempfile

    existing = get_config()
    merged = _merge_config_deep(existing, config)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    # 原子写:先写临时文件 → fsync → rename(原子)。并发安全(pid+nonce 唯一名)。
    fd, tmp = tempfile.mkstemp(dir=str(CONFIG_DIR), prefix=".config.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(yaml.dump(merged, default_flow_style=False, allow_unicode=True))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CONFIG_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def get_worktrees_root() -> Path:
    """Resolve the per-story workspace root (where D:/worktrees/<slug>/ lives).

    Priority: config.yaml `worktrees_root` → env STORY_WORKTREES_ROOT →
    platform default (``D:/worktrees`` on Windows, ``~/worktrees`` elsewhere).

    The directory itself is NOT created here — callers mkdir on demand.
    Per-story workspaces are subdirectories: ``<root>/<slug>/``. The planning
    LLM picks the ``slug`` (title-derived kebab-case); the orchestrator mkdirs
    it and points the code agent's cwd there.
    """
    cfg = get_config()
    raw = cfg.get("worktrees_root") or os.environ.get("STORY_WORKTREES_ROOT") or ""
    if raw:
        return Path(raw).expanduser()
    # platform default
    if os.name == "nt":
        return Path("D:/worktrees")
    return Path.home() / "worktrees"
