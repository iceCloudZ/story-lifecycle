"""Base adapter — defines the interface all CLI adapters must implement."""

import hashlib
import json
import os
import shlex
from abc import ABC, abstractmethod
from datetime import datetime, timezone


class BaseAdapter(ABC):
    """Abstract interface for AI coding CLI tools."""

    # PTY readiness marker (regex). When starting an interactive PTY, output is
    # polled until this matches before injecting the prompt — fixes slow-startup
    # idle (agent loads skills/indexing >2s, swallows the early injection; see
    # pty._wait_ready, real-run §7.1 follow-up). None → legacy fixed startup_delay
    # sleep. Subclasses override with their CLI's input-prompt regex.
    readiness_marker: str | None = None

    # How this adapter wants the seed prompt delivered at spawn:
    #   False (default, claude-style) → prompt baked into interactive_launch_cmd
    #     (e.g. `claude "query"`). PTY injection is OFF.
    #   True (ShellAdapter / kimi / codex) → interactive_launch_cmd ignores the
    #     prompt arg; the spawner pastes the seed via PTY after readiness_marker.
    # Lets the spawner branch on intent without isinstance() checks against
    # concrete adapter classes (the prior bug: kimi's base interactive_launch_cmd
    # silently dropped the prompt → empty kimi session).
    prompts_via_pty: bool = False

    @abstractmethod
    def switch_provider(self, provider: str) -> str | None:
        """Return the shell command to switch provider, or None if not needed."""
        ...

    @abstractmethod
    def launch_cmd(self, model: str) -> str:
        """Return the command to launch the CLI interactively in a session."""
        ...

    @abstractmethod
    def inject_prompt(self, prompt: str, story_key: str, stage: str) -> str | None:
        """Return the shell command to inject a prompt into the running CLI,
        or None if prompt injection is handled by ttyd.paste_text()."""
        ...

    # --- story<->session anchor (I2: agent-transcript-miner integration) ---
    # adapter.inject_prompt 启动会话时，追加写一个锚点到
    # <workspace>/.story/runs/<story_key>/anchors.jsonl，供 miner.link 精确回填
    # sessions.story_id（替代 cwd+ts 宽窗猜测）。单向：story-lifecycle 写，miner 读。
    # 不改 inject_prompt 核心逻辑，只在开头追加写一行 JSONL。
    @staticmethod
    def _anchor_adapter_name() -> str:
        """Best-effort adapter name for the anchor record.

        Subclasses override by setting ``name`` or passing it to
        write_anchor(); falls back to the class name lowercased.
        """
        return "adapter"

    def write_anchor(
        self,
        prompt: str,
        story_key: str,
        stage: str,
        cwd: str | None = None,
        workspace: str | None = None,
    ) -> str | None:
        """Append a story<->session anchor to
        ``<workspace>/.story/runs/<story_key>/anchors.jsonl``.

        Each line: ``{"story_key","stage","adapter","cwd","ts"(iso 精确),"prompt_hash"}``.

        cwd defaults to the current process working directory (the story's
        workspace when launched via the orchestrator). workspace defaults to
        cwd. Returns the anchor file path on success, None if it could not be
        written (anchor writing is best-effort and must never break injection).
        """
        try:
            cwd = cwd or os.getcwd()
            ws = workspace or cwd
            ws = os.path.normpath(str(ws))
            adapter_name = getattr(self, "name", None) or self._anchor_adapter_name()
            prompt_hash = hashlib.sha256(
                (prompt or "").encode("utf-8", errors="replace")
            ).hexdigest()[:16]
            anchor = {
                "story_key": story_key,
                "stage": stage,
                "adapter": adapter_name,
                "cwd": os.path.normpath(str(cwd)),
                # UTC（带 +00:00）以对齐 claude/codex transcript 里的会话 ts（也是 UTC）；
                # 否则 miner.link 按 date[:10] 比较，本地日 vs UTC 日会跨日错位导致绑定失败。
                "ts": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "prompt_hash": prompt_hash,
            }
            runs_dir = os.path.join(ws, ".story", "runs", story_key)
            os.makedirs(runs_dir, exist_ok=True)
            path = os.path.join(runs_dir, "anchors.jsonl")
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(anchor, ensure_ascii=False) + "\n")
            return path
        except OSError:
            # anchor writing must never break the core injection flow
            return None

    def headless_launch_cmd(self, model: str, prompt: str) -> list[str] | None:
        """Return command args for non-interactive headless execution.

        The prompt is piped via stdin, NOT passed as a CLI argument —
        avoids OS command-line length limits on long prompts.

        Returns None if the adapter does not support headless mode.
        Subclasses should override when their CLI has a native
        non-interactive execution flag (e.g. claude -p, codex -q).
        """
        return None

    def interactive_launch_cmd(
        self,
        model: str,
        prompt: str = "",
        session_id: str = "",
        session_name: str = "",
        resume: bool = False,
    ) -> list[str]:
        """Return argv for an interactive PTY process.

        ``prompt`` (optional): an initial prompt to seed the session with.
        Adapters whose CLI supports an initial-message argument (e.g.
        ``claude "query"``) should include it so the agent starts on the task
        without a separate PTY injection (which needs unreliable readiness
        detection). ``session_id``/``session_name``/``resume`` are for
        session-persistence (e.g. claude --session-id/--resume); ignored by base.
        """
        return shlex.split(self.launch_cmd(model), posix=os.name != "nt")

    def bypass_flags(self) -> list[str]:
        """Permission-bypass CLI flags(0d 权限源头堵)。

        codex/kimi 等在源头用 flag 自动批准权限提问(supervisor 专注答澄清/选择问题)。
        claude **不 bypass**(走 ``--permission-prompt-tool`` 由 supervisor 决策)。
        默认空 = 不 bypass(supervisor 兜底应答)。子类按 CLI 覆写。
        """
        return []

    def cleanup(self, story_key: str, stage: str):
        """Clean up temp files after stage completion. Override if needed."""
        pass

    def enter_session_cmd(self, session_name: str, workspace: str) -> str:
        """Command to create and enter a multiplexer session."""
        from ...infra.terminal import ttyd

        return ttyd.enter_session_cmd(session_name, workspace)
