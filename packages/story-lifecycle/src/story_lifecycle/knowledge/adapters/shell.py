"""ShellAdapter — config-driven adapter for any AI CLI tool."""

import re
import shlex
from pathlib import Path
from typing import Any

import yaml

from .base import BaseAdapter, SessionSpec

_CONFIG_PATH = Path.home() / ".story-lifecycle" / "adapters.yaml"


def _load_adapter_configs() -> dict[str, dict]:
    if not _CONFIG_PATH.exists():
        return {}
    try:
        raw = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


# Built-in readiness markers for well-known shell-driven CLIs, so they work
# out-of-the-box without each user hand-editing ~/.story-lifecycle/adapters.yaml.
# These are the distinctive strings each CLI prints once its interactive input
# box is ready (polled by pty._wait_ready before prompt injection). A yaml
# `readiness_marker` on the adapter overrides these defaults.
_DEFAULT_READINESS_MARKERS: dict[str, str] = {
    # kimi-code prints this banner once the TUI is up; `>` alone is too generic
    # (matches shell prompts). "Welcome to Kimi Code" is unique to kimi startup.
    "kimi": "Welcome to Kimi Code",
}

# Built-in session-id capture / resume 配置(Phase 0 抽象:从 planner.py 的
# _KIMI_SESSION_RE + 硬编码 -S 搬到这里)。kimi 在退出时吐
# `To resume this session: kimi -r session_<uuid>`(或旧 banner `Session: <uuid>`),
# 由 make_sid_capturer 正则捕获回填。yaml 的 exit_sid_regex / resume_flag 覆盖默认。
_DEFAULT_EXIT_SID_REGEX: dict[str, str] = {
    "kimi": r"(?:kimi\s+-r\s+|Session:\s*)(session_[0-9a-fA-F-]+)",
}
_DEFAULT_RESUME_FLAG: dict[str, list[str]] = {
    # kimi resume: `kimi -S <id>`。-S 是 --session 的短形式。
    "kimi": ["-S"],
}


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
        # Override the BaseAdapter class-attr None with a per-adapter marker.
        # Source priority: adapters.yaml `readiness_marker` → built-in default
        # for known CLIs → None (legacy 2s startup_delay).
        # Without this, shell-driven CLIs fall back to the 2s sleep, which
        # misses slow startup (kimi loads skills/indexing >2s) — the prompt gets
        # injected before the agent's input box is ready and is silently
        # swallowed (the "kimi opened but no instruction was pasted" symptom).
        cfg_marker = self._config.get("readiness_marker")
        if cfg_marker:
            self.readiness_marker = cfg_marker
        else:
            default_marker = _DEFAULT_READINESS_MARKERS.get(name.lower())
            if default_marker:
                self.readiness_marker = default_marker

    @property
    def name(self) -> str:  # noqa: D401 - short descriptor
        return self._name

    def switch_provider(self, provider: str) -> str | None:
        return None

    def launch_cmd(self, model: str) -> str:
        template = self._config.get("launch_cmd", "")
        return template.format(model=model)

    def interactive_launch_cmd(
        self,
        model: str,
        prompt: str = "",
        session_id: str = "",
        session_name: str = "",
        resume: bool = False,
    ) -> list[str]:
        """kimi/codex 交互式启动命令,支持 session 恢复。

        resume=True 且 session_id 非空 → 追加 CLI 的 resume 参数。
        kimi 用 ``-S <id>``(--session);其他 shell CLI 暂不支持 resume(忽略)。
        prompt 不进 command(走 PTY paste,见 start_session 的 pty_prompt),故这里忽略 prompt。
        """
        cmd = super().interactive_launch_cmd(
            model,
            prompt="",
            session_id=session_id,
            session_name=session_name,
            resume=resume,
        )
        # 加 model flag(若配置)和 bypass flags(若配置)。
        model_flag = self._config.get("model_flag")
        if model_flag and model:
            cmd += [model_flag, model]
        cmd += self.bypass_flags()
        # resume:config 的 resume_flag(kimi 默认 ["-S"]);其他 shell CLI 无 resume。
        # 优先级:yaml resume_flag → _DEFAULT_RESUME_FLAG → 无。
        if resume and session_id:
            resume_flag = (
                self._config.get("resume_flag")
                or _DEFAULT_RESUME_FLAG.get(self._name.lower())
            )
            if resume_flag:
                cmd += list(resume_flag) + [session_id]
        return cmd

    def start_session(
        self,
        model: str,
        prompt: str = "",
        session_id: str = "",
        session_name: str = "",
        resume: bool = False,
    ) -> SessionSpec:
        """shell CLI 的 SessionSpec:prompt 走 PTY paste(pty_prompt),command 由
        interactive_launch_cmd 构建(含 resume 的 -S <id> for kimi;Phase 0 起由
        config/yaml resume_flag 驱动)。

        与 base.start_session 的区别:base 用 launch_cmd 直接 split(绕过
        interactive_launch_cmd),这里走 interactive_launch_cmd 才能把 resume_flag 带上。
        """
        command = self.interactive_launch_cmd(
            model,
            prompt="",
            session_id=session_id,
            session_name=session_name,
            resume=resume,
        )
        return SessionSpec(
            command=command,
            pty_prompt=prompt,
            readiness_marker=self.readiness_marker,
            session_id=session_id,
            resume=resume,
        )

    def bypass_flags(self) -> list[str]:
        # 从 adapters.yaml 的 bypass_flags 读(kimi: ["--auto"] / ["-y"];aider: [])。
        return list(self._config.get("bypass_flags", []) or [])

    def _exit_sid_pattern(self) -> re.Pattern | None:
        # 优先级:yaml exit_sid_regex → _DEFAULT_EXIT_SID_REGEX(kimi)→ None。
        raw = self._config.get("exit_sid_regex") or _DEFAULT_EXIT_SID_REGEX.get(
            self._name.lower()
        )
        return re.compile(raw) if raw else None

    def make_sid_capturer(self, story_key: str, stage: str, cwd: str | None = None,
                          since_ts: str | None = None):
        """kimi 退出时吐的 resume 行捕获。

        kimi 退出时打印 ``To resume this session: kimi -r session_<uuid>``(也兼容
        旧 banner ``Session: session_<uuid>``)。返回的 on_output 回调累积 clean_exit_pty
        drain 的输出,正则命中即回填 DB(幂等、命中后短路)。

        搬自 planner._make_kimi_sid_capturer(DESIGN-session-pty-id-model.md §3.5 /
        问题 9):此前只有 banner 正则且格式不符,kimi resume 在 0.29.0 上从未工作过,
        改在退出时捕获(时机确定、格式准)。best-effort:kimi 崩溃没吐就捕获不到,
        下次当新会话(不崩,只是不省 token)。
        """
        pattern = self._exit_sid_pattern()
        if pattern is None:
            return None

        buf = ""
        done = False

        def _on_output(text: str) -> None:
            nonlocal buf, done
            if done:
                return
            buf += text
            m = pattern.search(buf)
            if m:
                captured = m.group(1)
                done = True
                try:
                    from ...infra.db import models as _sd

                    _sd.set_session_id(story_key, stage, self._name, captured)
                except Exception as exc:  # best-effort,绝不拖垮 clean_exit
                    import logging

                    logging.getLogger(__name__).warning(
                        "[%s] %s session backfill failed (%s); resume disabled for stage=%s",
                        story_key, self._name, exc, stage,
                    )

        return _on_output

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
