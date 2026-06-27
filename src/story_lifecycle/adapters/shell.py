"""ShellAdapter — config-driven adapter for any AI CLI tool."""

from pathlib import Path
from typing import Any

import yaml

from .base import BaseAdapter

_CONFIG_PATH = Path.home() / ".story-lifecycle" / "adapters.yaml"


def _load_adapter_configs() -> dict[str, dict]:
    if not _CONFIG_PATH.exists():
        return {}
    try:
        raw = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


class ShellAdapter(BaseAdapter):
    """Generic shell adapter — driven by adapters.yaml config.

    Config format:

        aider:
          launch_cmd: "aider --model {model}"
          inject_method: stdin

        codex:
          launch_cmd: "codex --model {model}"
          inject_method: stdin
    """

    def __init__(self, config: dict[str, Any] | None = None, name: str = "shell"):
        self._config = config or {}
        self._name = name

    @property
    def name(self) -> str:  # noqa: D401 - short descriptor
        return self._name

    def switch_provider(self, provider: str) -> str | None:
        return None

    def launch_cmd(self, model: str) -> str:
        template = self._config.get("launch_cmd", "")
        return template.format(model=model)

    def inject_prompt(self, prompt: str, story_key: str, stage: str) -> str | None:
        # I2: record anchor (best-effort, before any core logic).
        self.write_anchor(prompt, story_key, stage)
        method = self._config.get("inject_method", "paste")
        if method == "stdin":
            from pathlib import Path
            import tempfile

            tmp = Path(tempfile.gettempdir()) / f"story-prompt-{story_key}-{stage}.txt"
            tmp.write_text(prompt, encoding="utf-8")
            return f"cat {tmp}"
        return None

    def cleanup(self, story_key: str, stage: str):
        pass
