"""ShellAdapter — config-driven adapter for any AI CLI tool."""

import shlex
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

    def headless_launch_cmd(self, model: str, prompt: str) -> list[str] | None:
        """Headless mode launch command.

        Default: ``[binary, '-p']`` — prompt piped via stdin (works for claude).
        If config has ``stdin_to_prompt_arg: true``: wraps in a Python subprocess
        that reads stdin → passes as the -p CLI argument (for kimi, where -p
        takes an argument, not stdin).
        """
        binary = self._config.get("binary", self._name)

        if self._config.get("stdin_to_prompt_arg"):
            # binary is interpolated into a Python string literal inside the
            # -c source; use its repr() so a quote/apostrophe in binary cannot
            # break out of the literal and execute arbitrary code.
            wrapper = (
                f"import sys, subprocess; "
                f"subprocess.run([{binary!r}, '-p', sys.stdin.read()])"
            )
            return ["python", "-c", wrapper]

        cmd = [binary, "-p"]
        model_flag = self._config.get("model_flag")
        if model_flag and model:
            cmd += [model_flag, model]
        return cmd

    def inject_prompt(self, prompt: str, story_key: str, stage: str) -> str | None:
        # I2: record anchor (best-effort, before any core logic).
        self.write_anchor(prompt, story_key, stage)
        method = self._config.get("inject_method", "paste")
        if method == "stdin":
            from pathlib import Path
            import tempfile

            from ...infra.story_paths import safe_segment

            tmp = Path(tempfile.gettempdir()) / (
                f"story-prompt-{safe_segment(story_key)}-{safe_segment(stage)}.txt"
            )
            tmp.write_text(prompt, encoding="utf-8")
            return f"cat {shlex.quote(str(tmp))}"
        return None

    def cleanup(self, story_key: str, stage: str):
        pass
