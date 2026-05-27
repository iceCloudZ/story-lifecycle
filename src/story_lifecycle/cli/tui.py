"""Interactive TUI board — Textual App for story management."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from datetime import datetime

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Footer, Static, Input, Button
from textual.reactive import reactive

from ..db import models as db
from ..orchestrator.graph import set_tui_app, take_plan_done, take_terminal_opened
from ..orchestrator.service import (
    get_story_cli_model,
    fail_story,
    skip_stage,
    create_and_start_story,
    delete_story,
)
from ..orchestrator.nodes import load_profile
from ..terminal import ttyd
from ..orchestrator.entry import (
    StageEntryAction,
    StageEntryState,
    TtydSessionBackend,
    resolve_stage_state,
    decide_action,
    entry_action_notice,
    has_stage_done,
    validate_stage_done,
    DoneStatus,
    WorkspaceState,
    cli_exit_marker_path,
    resolve_cli_exit_state,
    CliExitState,
)


STORY_HOME = Path.home() / ".story-lifecycle"
_FINISHED_STATUSES = frozenset({"completed", "failed", "aborted"})


def _tui_debug(event: str, **fields):
    """Append TUI diagnostics to ~/.story-lifecycle/tui.log."""
    try:
        STORY_HOME.mkdir(parents=True, exist_ok=True)
        details = " ".join(f"{key}={value!r}" for key, value in fields.items())
        line = f"{datetime.now().isoformat(timespec='seconds')} {event}"
        if details:
            line += f" {details}"
        with (STORY_HOME / "tui.log").open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _prepare_terminal_for_child():
    """Reset terminal state before handing control to an interactive child."""
    try:
        if os.name == "nt":
            from textual.drivers import win32

            input_mode_before = win32.get_console_mode(sys.__stdin__)
            output_mode_before = win32.get_console_mode(sys.__stdout__)
            win32.set_console_mode(
                sys.__stdin__,
                win32.ENABLE_PROCESSED_INPUT
                | win32.ENABLE_LINE_INPUT
                | win32.ENABLE_ECHO_INPUT
                | win32.ENABLE_EXTENDED_FLAGS
                | win32.ENABLE_INSERT_MODE,
            )
            win32.set_console_mode(
                sys.__stdout__,
                output_mode_before
                | win32.ENABLE_PROCESSED_OUTPUT
                | win32.ENABLE_WRAP_AT_EOL_OUTPUT
                | win32.ENABLE_VIRTUAL_TERMINAL_PROCESSING,
            )
            input_mode_after = win32.get_console_mode(sys.__stdin__)
            output_mode_after = win32.get_console_mode(sys.__stdout__)
            _tui_debug(
                "prepare_terminal_windows_modes",
                input_before=input_mode_before,
                input_after=input_mode_after,
                output_before=output_mode_before,
                output_after=output_mode_after,
            )
        sys.__stdout__.write(
            "\x1b[?1049l"
            "\x1b[?25h"
            "\x1b[0m"
            "\x1b[?1000l"
            "\x1b[?1003l"
            "\x1b[?1015l"
            "\x1b[?1006l"
            "\x1b[?2004l"
            "\033[?1004l"
        )
        sys.__stdout__.flush()
        _tui_debug("prepare_terminal_done")
    except Exception as exc:
        _tui_debug(
            "prepare_terminal_exception",
            error_type=type(exc).__name__,
            error=str(exc),
        )


class StoryCard(Static):
    """A single story card in the board."""

    DEFAULT_CSS = """
    StoryCard {
        padding: 1 2;
        height: auto;
    }
    StoryCard.selected {
        background: $boost;
    }
    """

    def __init__(
        self,
        story: dict,
        selected: bool = False,
        collapsed: bool = False,
        sub_count: int = 0,
    ):
        self.story = story
        self._selected = selected
        self._collapsed = collapsed
        self._sub_count = sub_count
        super().__init__(classes="selected" if selected else "")

    def set_selected(self, selected: bool):
        self._selected = selected
        self.set_class(selected, "selected")
        self.refresh()

    def render(self) -> str:
        s = self.story
        key = s["story_key"]
        title = (s.get("title") or "")[:60]
        status = s.get("status", "active")
        stage = s.get("current_stage", "")
        retries = s.get("execution_count", 0)
        last_error = s.get("last_error", "")
        profile_name = s.get("profile", "minimal")
        is_child = bool(s.get("parent_key"))
        sub_type = s.get("sub_type") or ""
        type_badge = ""
        if sub_type:
            colors = {
                "bug-fix": "red",
                "integration": "yellow",
                "refinement": "blue",
                "redo": "orange",
            }
            color = colors.get(sub_type, "grey")
            type_badge = f" [{color}][{sub_type}][/{color}]"

        badge = {
            "active": "[bold green]> active[/]",
            "paused": "[bold yellow]|| paused[/]",
            "blocked": "[bold red]X blocked[/]",
            "completed": "[dim green]OK done[/]",
            "waiting_subtasks": "[bold magenta]≡ waiting subs[/]",
        }.get(status, status)

        # Gate-paused stories get a distinct badge
        if status == "paused":
            try:
                import json as _json

                ctx = _json.loads(s.get("context_json") or "{}")
            except Exception:
                ctx = {}
            if ctx.get("last_gate_decision_id"):
                badge = "[bold yellow]|| review gate[/]"

        try:
            profile = load_profile(profile_name)
            stages = list(profile.get("stages", {}).keys())
            bar = _render_stage_bar(stages, stage)
        except FileNotFoundError:
            bar = stage

        cli_info = get_story_cli_model(key)
        cli_line = f"  [dim]CLI: {cli_info['cli']} · Model: {cli_info['model']}[/]"

        collapse_info = ""
        if self._sub_count > 0:
            arrow = "▸" if self._collapsed else "▾"
            collapse_info = f" [dim]({self._sub_count} 个子故事 {arrow})[/]"

        cursor = "[bold cyan]▸[/] " if self._selected else "  "
        indent = "  └─ " if is_child else ""

        lines = [
            f"{cursor}{indent}[bold cyan]{key}[/]{type_badge}{collapse_info}  {title}",
            f"  {bar}  {badge}  [dim]retries: {retries}[/]",
            cli_line,
        ]

        if last_error and status == "blocked":
            lines.append(f"  [dim red]↳ {last_error[:80]}[/]")

        return "\n".join(lines)


def _render_stage_bar(stages: list[str], current: str) -> str:
    """Render stage progress bar, truncating if too many stages."""
    if len(stages) > 5:
        idx = stages.index(current) if current in stages else 0
        show = []
        if idx > 0:
            show.append("...")
            show.append(f"● {stages[idx - 1]}")
        show.append(f"◉ [bold]{current}[/]")
        if idx < len(stages) - 1:
            show.append(f"○ {stages[idx + 1]}")
            if idx + 1 < len(stages) - 1:
                show.append("...")
        return " → ".join(show)

    parts = []
    idx = stages.index(current) if current in stages else -1
    for i, s in enumerate(stages):
        if s == current:
            parts.append(f"◉ [bold]{s}[/]")
        elif i < idx:
            parts.append(f"● {s}")
        else:
            parts.append(f"○ {s}")
    return " → ".join(parts)


def _render_detail(story: dict) -> str:
    """Render expanded detail for a story."""
    import json

    s = story
    key = s["story_key"]
    lines = [
        f"[bold]{key}[/] — {s.get('title', '')}",
        f"  Stage:     {s.get('current_stage', '')}",
        f"  Status:    {s.get('status', '')}",
        f"  Profile:   {s.get('profile', 'minimal')}",
        f"  Workspace: {s.get('workspace', '')}",
        f"  Retries:   {s.get('execution_count', 0)}",
        f"  Created:   {s.get('created_at', '')}",
        f"  Updated:   {s.get('updated_at', '')}",
    ]

    if s.get("last_error"):
        lines.append(f"  [red]Error: {s['last_error']}[/]")

    try:
        ctx = json.loads(s.get("context_json") or "{}")
        if ctx:
            lines.append("  Context:")
            for k, v in ctx.items():
                val = str(v)
                if len(val) > 500:
                    val = val[:500] + "..."
                lines.append(f"    {k}: {val}")
    except json.JSONDecodeError:
        pass

    # Gate status (P0 — review gate visibility)
    try:
        ctx = json.loads(s.get("context_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        ctx = {}
    if ctx.get("last_gate_decision_id") and s.get("status") == "paused":
        lines.append("  [bold yellow]Review Gate:[/]")
        lines.append(f"    Decision: {ctx.get('last_gate_decision', 'wait_confirm')}")
        lines.append(f"    Reason: {ctx.get('last_gate_reason_code', 'unknown')}")
        if s.get("last_error"):
            lines.append(f"    Message: {s['last_error']}")
        report = ctx.get("last_gate_report_path", "")
        if report:
            lines.append(f"    Report: {report}")
        gate_events = [
            e
            for e in db.get_story_events(key)
            if e.get("event_type") == "gate_decision"
        ]
        if gate_events:
            latest = gate_events[-1]
            p = latest.get("payload", {})
            if isinstance(p, str):
                try:
                    p = json.loads(p)
                except (json.JSONDecodeError, TypeError):
                    p = {}
            if isinstance(p, dict):
                reviewer = p.get("reviewer", {})
                if reviewer:
                    reviewer_line = reviewer.get("kind", "?")
                    if reviewer.get("model"):
                        reviewer_line += f" / {reviewer['model']}"
                    lines.append(f"    Reviewer: {reviewer_line}")
                lines.append(
                    f"    Executor attempts: {p.get('executor_attempt_count', '?')}"
                )
                lines.append(f"    Review rounds: {p.get('review_round_count', '?')}")
                allowed = p.get("allowed_actions", [])
                if allowed:
                    lines.append(f"    [bold green]Actions: {', '.join(allowed)}[/]")

    # Evaluator loop activity
    try:
        loop_events = [
            e
            for e in db.get_story_events(key)
            if e.get("event_type", "").startswith("evaluator_loop_")
        ]
        if loop_events:
            lines.append("  [bold cyan]Evaluator Loop:[/]")
            for ev in loop_events[-5:]:
                p = ev.get("payload", {})
                if isinstance(p, str):
                    try:
                        p = json.loads(p)
                    except (json.JSONDecodeError, TypeError):
                        p = {}
                etype = ev["event_type"]
                if etype == "evaluator_loop_round":
                    lt = p.get("loop_type", "?")
                    stg = p.get("stage", "?")
                    rnd = p.get("round_id", "?")
                    dec = p.get("decision", "?")
                    color = (
                        "green"
                        if dec == "pass"
                        else "yellow"
                        if dec == "revise"
                        else "red"
                    )
                    extra = ""
                    if p.get("no_progress"):
                        extra = " [bold red]NO PROGRESS[/]"
                    findings = p.get("findings", {})
                    n_rep = len(findings.get("repeated", []))
                    n_new = len(findings.get("new", []))
                    n_res = len(findings.get("resolved", []))
                    parts = []
                    if n_rep:
                        parts.append(f"repeated={n_rep}")
                    if n_new:
                        parts.append(f"new={n_new}")
                    if n_res:
                        parts.append(f"resolved={n_res}")
                    detail = f", {', '.join(parts)}" if parts else ""
                    lines.append(
                        f"    {lt}/{stg} round {rnd}: [{color}]{dec}[/{color}]{detail}{extra}"
                    )
                elif etype == "evaluator_loop_completed":
                    lt = p.get("loop_type", "?")
                    dec = p.get("decision", "?")
                    reason = p.get("reason", "")
                    color = (
                        "green"
                        if dec == "pass"
                        else "yellow"
                        if dec == "revise"
                        else "red"
                    )
                    lines.append(
                        f"    {lt} completed: [{color}]{dec}[/{color}] ({reason})"
                    )
                elif etype == "evaluator_loop_started":
                    lt = p.get("loop_type", "?")
                    mx = p.get("max_rounds", "?")
                    lines.append(f"    {lt} started (max {mx} rounds)")
                elif etype == "evaluator_loop_fallback":
                    lines.append(
                        f"    [yellow]fallback: {p.get('from_mode', '')} → {p.get('to_mode', '')} ({p.get('reason', '')})[/]"
                    )
    except Exception:
        pass

    # Quality findings panel
    try:
        from ..db import models as qdb

        findings = qdb.get_open_findings(key)
        if findings:
            lines.append("  [bold red]Quality Findings:[/]")
            for f in findings[:5]:
                sev = f["severity"].upper()
                color = (
                    "red" if sev == "HIGH" else "yellow" if sev == "MEDIUM" else "green"
                )
                lines.append(
                    f"    [{color}]● {sev}[/{color}] {f['category']}: {f['description']}"
                )
        patterns = qdb.get_active_learned_patterns(limit=3)
        if patterns:
            lines.append("  [bold cyan]Learned Patterns:[/]")
            for p in patterns:
                lines.append(f"    ◆ {p['pattern']}")
    except Exception:
        pass

    return "\n".join(lines)


class NewStoryDialog(ModalScreen):
    """Modal dialog for creating a new story."""

    CSS = """
    NewStoryDialog {
        align: center middle;
    }
    #dialog {
        width: 60;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: thick $accent;
    }
    #dialog Input {
        margin: 1 0;
    }
    #dialog Static {
        margin: 0 0 1 0;
    }
    #btn-row {
        height: auto;
        margin-top: 1;
    }
    #btn-row Button {
        margin-right: 1;
    }
    """

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog"):
            yield Static("[bold]New Story[/]")
            yield Static("Story Key:")
            yield Input(placeholder="e.g. FEATURE-001", id="input-key")
            yield Static("Title:")
            yield Input(placeholder="e.g. Add user auth", id="input-title")
            yield Static("PRD file path (optional):")
            yield Input(
                placeholder="e.g. prd/FEATURE-001.md",
                id="input-prd",
            )
            with Horizontal(id="btn-row"):
                yield Button("Create", variant="success", id="btn-create")
                yield Button("Cancel", variant="default", id="btn-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-create":
            key = self.query_one("#input-key", Input).value.strip()
            title = self.query_one("#input-title", Input).value.strip()
            prd = self.query_one("#input-prd", Input).value.strip()
            if key:
                self.dismiss((key, title, prd))
            else:
                self.query_one("#input-key", Input).focus()
        else:
            self.dismiss(None)

    def on_mount(self):
        self.query_one("#input-key", Input).focus()


class SubStoryDialog(ModalScreen):
    """Modal dialog for creating a sub-story."""

    CSS = """
    SubStoryDialog {
        align: center middle;
    }
    #sub-dialog {
        width: 60;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: thick $accent;
    }
    #sub-dialog Input {
        margin: 1 0;
    }
    #sub-dialog Static {
        margin: 0 0 1 0;
    }
    #sub-btn-row {
        height: auto;
        margin-top: 1;
    }
    #sub-btn-row Button {
        margin-right: 1;
    }
    """

    def __init__(self, parent_key: str):
        self._parent_key = parent_key
        self._selected_type_idx = 0
        from ..cli.setup import get_sub_types

        self._type_configs = get_sub_types()
        self._type_keys = list(self._type_configs.keys())
        super().__init__()

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="sub-dialog"):
            yield Static(f"[bold]New Sub-story for {self._parent_key}[/]")
            yield Static("Type:")
            for i, key in enumerate(self._type_keys):
                cfg = self._type_configs[key]
                label = cfg.get("label", key)
                marker = ">" if i == 0 else " "
                yield Static(f"  {marker} {label} ({key})")
            yield Static("Custom type (optional, overrides selection):")
            yield Input(
                placeholder="e.g. hotfix (leave empty to use selection)",
                id="input-custom-type",
            )
            yield Static("Start Stage (empty = auto):")
            yield Input(
                placeholder="e.g. implement (auto-derived from type)", id="input-stage"
            )
            yield Static("Description:")
            first_template = list(self._type_configs.values())[0].get(
                "description_template", ""
            )
            yield Input(value=first_template, id="input-desc")
            with Horizontal(id="sub-btn-row"):
                yield Button("Create", variant="success", id="btn-sub-create")
                yield Button("Cancel", variant="default", id="btn-sub-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-sub-create":
            desc = self.query_one("#input-desc", Input).value.strip()
            if not desc:
                self.query_one("#input-desc", Input).focus()
                return

            custom_type = self.query_one("#input-custom-type", Input).value.strip()
            if custom_type:
                sub_type = custom_type
                default_stage = ""
            else:
                selected_key = self._type_keys[self._selected_type_idx]
                sub_type = selected_key
                default_stage = self._type_configs[selected_key].get(
                    "default_start_stage", ""
                )

            custom_stage = self.query_one("#input-stage", Input).value.strip()
            self.dismiss(
                {
                    "sub_type": sub_type,
                    "start_stage": custom_stage or default_stage or None,
                    "description": desc,
                }
            )
        else:
            self.dismiss(None)

    def on_mount(self):
        self.query_one("#input-desc", Input).focus()


class ConfirmDialog(ModalScreen):
    """Modal dialog for confirming a destructive action."""

    CSS = """
    ConfirmDialog {
        align: center middle;
    }
    #confirm-dialog {
        width: 50;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: thick $error;
    }
    #confirm-dialog Static {
        margin: 0 0 1 0;
    }
    #confirm-btn-row {
        height: auto;
        margin-top: 1;
    }
    #confirm-btn-row Button {
        margin-right: 1;
    }
    """

    def __init__(self, message: str):
        self._message = message
        super().__init__()

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="confirm-dialog"):
            yield Static(f"[bold red]{self._message}[/]")
            yield Static("This cannot be undone.")
            with Horizontal(id="confirm-btn-row"):
                yield Button("Delete", variant="error", id="btn-confirm-yes")
                yield Button("Cancel", variant="default", id="btn-confirm-no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "btn-confirm-yes")


class LoadingScreen(ModalScreen):
    """加载中弹窗 — 显示 spinner 等待异步操作完成。"""

    def compose(self) -> ComposeResult:
        yield Static("[bold cyan]正在从 TAPD 拉取待办...[/]", id="loading-text")

    BINDINGS = []


class InboxScreen(ModalScreen):
    """待办收件箱 — 显示外部平台拉取的待办条目。"""

    BINDINGS = [
        Binding("escape", "close_inbox", "Close"),
        Binding("r", "refresh_inbox", "Refresh"),
    ]

    def __init__(self, items: list):
        self._items = items
        self._selected: set[int] = set()
        self._cursor = 0
        super().__init__()

    def compose(self) -> ComposeResult:
        with Vertical(id="inbox-container"):
            yield Static("[bold]待办收件箱[/]", id="inbox-title")
            yield Static("", id="inbox-list")
            with Horizontal(id="inbox-btn-row"):
                yield Button("确认创建", variant="success", id="btn-inbox-confirm")
                yield Button("AI增强PRD", variant="primary", id="btn-inbox-ai-prd")
                yield Button("取消", variant="default", id="btn-inbox-cancel")

    def on_mount(self) -> None:
        self._refresh_list()

    def _refresh_list(self):
        from rich.text import Text

        lines: list[Text] = []
        for i, item in enumerate(self._items):
            check = "✓" if i in self._selected else " "
            cursor = ">" if i == self._cursor else " "
            type_tag = "需求" if item.item_type == "requirement" else "Bug"
            display_id = item.extra.get("short_id", item.id) if item.extra else item.id
            line = Text(
                f"  {cursor} [{check}] [{type_tag}] {display_id}  {item.title}  ({item.source})"
            )
            lines.append(line)
        self.query_one("#inbox-list", Static).update(Text("\n").join(lines))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-inbox-confirm":
            selected = [self._items[i] for i in sorted(self._selected)]
            self.dismiss([("normal", item) for item in selected])
        elif event.button.id == "btn-inbox-ai-prd":
            selected = [self._items[i] for i in sorted(self._selected)]
            self.dismiss([("ai_prd", item) for item in selected])
        else:
            self.dismiss([])

    def action_close_inbox(self):
        self.dismiss([])

    def action_refresh_inbox(self):
        pass  # Could re-fetch, but for P0 just do nothing

    def key_up(self):
        if self._cursor > 0:
            self._cursor -= 1
            self._refresh_list()

    def key_down(self):
        if self._cursor < len(self._items) - 1:
            self._cursor += 1
            self._refresh_list()

    def key_space(self):
        if self._cursor in self._selected:
            self._selected.discard(self._cursor)
        else:
            self._selected.add(self._cursor)
        self._refresh_list()

    def key_enter(self):
        if self._cursor not in self._selected:
            self._selected.add(self._cursor)
        self._refresh_list()
        selected = [self._items[i] for i in sorted(self._selected)]
        self.dismiss([("normal", item) for item in selected])


class ParentSelectDialog(ModalScreen):
    """Bug 关联父故事的手动选择对话框。"""

    def __init__(self, bug_title: str, stories: list[dict]):
        self._bug_title = bug_title
        self._stories = stories
        self._cursor = 0
        super().__init__()

    def compose(self) -> ComposeResult:
        with Vertical(id="parent-select-container"):
            yield Static("[bold]选择父故事[/]", id="parent-title")
            yield Static(f"Bug: {self._bug_title}", id="parent-desc")
            yield Static("", id="parent-list")
            with Horizontal(id="parent-btn-row"):
                yield Button("确认", variant="success", id="btn-parent-confirm")
                yield Button("独立创建", variant="warning", id="btn-parent-standalone")
                yield Button("取消", variant="default", id="btn-parent-cancel")

    def on_mount(self) -> None:
        self._refresh_list()

    def _refresh_list(self):
        lines = []
        for i, s in enumerate(self._stories):
            cursor = ">" if i == self._cursor else " "
            key = s.get("story_key", "")
            title = s.get("title", "")
            lines.append(f"  {cursor} {key}  {title}")
        self.query_one("#parent-list", Static).update("\n".join(lines))

    def key_up(self):
        if self._cursor > 0:
            self._cursor -= 1
            self._refresh_list()

    def key_down(self):
        if self._cursor < len(self._stories) - 1:
            self._cursor += 1
            self._refresh_list()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-parent-confirm" and self._stories:
            s = self._stories[self._cursor]
            self.dismiss(s.get("story_key"))
        elif event.button.id == "btn-parent-standalone":
            self.dismiss(None)
        else:
            self.dismiss("")

    def key_enter(self):
        if self._stories:
            s = self._stories[self._cursor]
            self.dismiss(s.get("story_key"))


class CopilotDialog(ModalScreen):
    """Modal dialog for asking Copilot a question."""

    CSS = """
    CopilotDialog {
        align: center middle;
    }
    #copilot-dialog {
        width: 70;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: thick $accent;
    }
    #copilot-dialog Input {
        margin: 1 0;
    }
    """

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="copilot-dialog"):
            yield Static("[bold]Ask Copilot[/]")
            yield Static(
                "输入你的问题，Copilot 将分析诊断数据包并提供建议（只读，不修改工作流状态）。"
            )
            yield Input(
                placeholder="e.g. 为什么 Story 卡住了？应该怎么排查？",
                id="copilot-input",
            )
            yield Static("[dim]Enter 提交 · Esc 取消[/]")

    def on_mount(self):
        self.query_one("#copilot-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted):
        if event.value.strip():
            self.dismiss(event.value.strip())
        else:
            self.dismiss(None)

    def key_escape(self):
        self.dismiss(None)


class StoryBoardApp(App):
    """Interactive story board TUI."""

    CSS = """
    Screen {
        layout: vertical;
        background: $surface;
    }

    #header-bar {
        height: 5;
        padding: 1 2;
        background: $boost;
        border-bottom: solid $accent;
    }

    #body-row {
        height: 1fr;
    }

    #left-pane {
        width: 1fr;
        height: 100%;
    }

    #plan-panel {
        height: 0;
        padding: 0;
        display: none;
    }
    #plan-panel.visible {
        height: auto;
        max-height: 14;
        padding: 1 2;
        background: $panel;
        border-bottom: solid $accent;
        display: block;
    }

    #story-list {
        height: 1fr;
        padding: 0;
        overflow-y: auto;
    }

    #detail-panel {
        height: 0;
        padding: 1 2;
        background: $panel;
        border-top: tall $accent;
        display: none;
    }
    #detail-panel.visible {
        height: auto;
        max-height: 14;
        display: block;
    }

    #diagnostics-panel {
        width: 44;
        min-width: 34;
        max-width: 56;
        height: 100%;
        padding: 1 2;
        border-left: solid $accent;
        background: $panel;
        overflow-y: auto;
    }

    #diagnostics-panel.hidden {
        display: none;
    }

    #footer-bar {
        height: 1;
        padding: 0 2;
        color: $text-muted;
    }

    Footer {
        dock: bottom;
    }
    """

    BINDINGS = [
        Binding("up", "cursor_up", "Up", key_display="↑"),
        Binding("down", "cursor_down", "Down", key_display="↓"),
        Binding("enter", "open_action_menu", "Actions"),
        Binding("n", "new_story", "New"),
        Binding("e", "enter_terminal", "Enter"),
        Binding("d", "toggle_detail", "Detail"),
        Binding("s", "skip_stage", "Skip"),
        Binding("f", "fail_story", "Fail"),
        Binding("r", "resume_story", "Resume"),
        Binding("shift+r", "refresh", "Refresh", key_display="R"),
        Binding("f5", "refresh", "Refresh"),
        Binding("x", "delete_story", "Delete"),
        Binding("shift+n", "new_sub_story", "Sub", key_display="N"),
        Binding("a", "abort_story", "Abort"),
        Binding("shift+a", "accept_risk_advance", "Accept Risk", key_display="A"),
        Binding("c", "toggle_collapse", "Fold"),
        Binding("shift+d", "run_doctor", "Doctor", key_display="D"),
        Binding("shift+s", "run_setup", "Setup", key_display="S"),
        Binding("i", "show_inbox", "Inbox"),
        Binding("question_mark", "help", "Help", key_display="?"),
        Binding("o", "toggle_diagnostics", "Diag", key_display="o"),
        Binding("y", "ask_copilot", "Ask Copilot", key_display="y"),
        Binding("1", "copilot_action_1", ""),
        Binding("2", "copilot_action_2", ""),
        Binding("3", "copilot_action_3", ""),
        Binding("p", "package_story_diagnostics", "Pkg Story", key_display="p"),
        Binding("shift+p", "package_global_diagnostics", "Pkg Global", key_display="P"),
        Binding("q", "quit", "Quit"),
    ]

    selected_index: reactive[int] = reactive(0)
    stories: reactive[list[dict]] = reactive([])

    def __init__(self):
        super().__init__()
        self._source_enabled = False
        self._pending_items: list = []
        self._pending_attach_args: list[str] | None = None
        self._session_backend = TtydSessionBackend()
        self._show_diagnostics = True
        self._copilot_loading = False
        self._copilot_question = ""
        self._copilot_result = None
        self._copilot_loading = False
        self._copilot_question = ""
        self._copilot_result: dict | None = None

    def compose(self) -> ComposeResult:
        yield Static(id="header-bar")
        yield Static(id="plan-panel")
        with Horizontal(id="body-row"):
            with Vertical(id="left-pane"):
                yield VerticalScroll(id="story-list")
                yield Static(id="completed-section")
                yield Static(id="detail-panel")
            yield Static(id="diagnostics-panel")
        yield Static(id="footer-bar")
        yield Footer()

    # 4x4 braille grid spinner — 3-dot trail rotating clockwise
    _SPINNER_FRAMES: list[str] = []
    for _off in range(12):
        _bits = [0, 0]
        for _i in range(3):
            _ci, _b = [
                (0, 0x01),
                (0, 0x08),
                (1, 0x01),
                (1, 0x08),  # top row
                (1, 0x10),
                (1, 0x20),  # right col
                (1, 0x80),
                (1, 0x40),  # bottom-right
                (0, 0x80),
                (0, 0x40),  # bottom-left
                (0, 0x04),
                (0, 0x02),  # left col
            ][(_off + _i) % 12]
            _bits[_ci] |= _b
        _SPINNER_FRAMES.append(chr(0x2800 + _bits[0]) + chr(0x2800 + _bits[1]))

    def on_mount(self):
        set_tui_app(self)
        self._watchdog_interval = 3
        self._show_detail = False
        self._show_diagnostics = True
        self._collapsed_parents: set[str] = set()
        self._plan_story_key = ""
        self._spinner_idx = -1  # -1 = stopped
        self._plan_start_time = 0.0
        self.refresh_stories()
        self.set_interval(5, self.refresh_stories)
        self.set_interval(3, self.watchdog_check)
        self.set_interval(0.08, self.tick_spinner)

        # Startup sweep: advance all stories with existing done files
        self._startup_sweep()

        # Source polling
        try:
            from ..cli.setup import get_config

            _config = get_config()
            _source_config = _config.get("story_source", {})
            if _source_config.get("enabled"):
                self._source_enabled = True
                _poll_interval = _source_config.get("poll_interval", 300)
                self.set_interval(_poll_interval, self._poll_source)
        except Exception:
            pass

        # Hide diagnostics on narrow screens at startup
        if self.size.width < 120:
            self._show_diagnostics = False
            self.query_one("#diagnostics-panel").set_class(True, "hidden")

    def _visible_stories(self) -> list[dict]:
        """Return stories visible after collapse filtering."""
        result = []
        for s in self.stories:
            pk = s.get("parent_key")
            if pk and pk in self._collapsed_parents:
                continue
            result.append(s)
        return result

    def refresh_stories(self):
        self.stories = db.list_active_stories()
        self._completed_stories = db.list_completed_stories()
        self._render()

    def _render(self, full: bool = True):
        from .setup import get_config

        config = get_config()
        provider = config.get("provider", "N/A")
        router_status = f"enabled ({provider})"
        active = len([s for s in self.stories if s["status"] == "active"])

        header = self.query_one("#header-bar")
        completed = getattr(self, "_completed_stories", [])
        completed_count = len(completed)
        from importlib.metadata import version as _pkg_version

        v = _pkg_version("story-lifecycle")
        header.update(
            "\n"
            "  [bold cyan]◆[/] [bold white]Story[/][bold cyan]Lifecycle[/] "
            f"[dim]v{v}[/] [dim]│[/] Router: {router_status} [dim]│[/] Stories: {active} active"
            f"{f' [dim]│[/] {completed_count} completed' if completed_count else ''}"
        )

        if full:
            story_list = self.query_one("#story-list")
            story_list.remove_children()
            if not self.stories:
                story_list.mount(
                    Static("[dim]No active stories. Press [[n]] to create one.[/]")
                )
            else:
                display_idx = 0
                for i, s in enumerate(self.stories):
                    pk = s.get("parent_key")
                    if pk and pk in self._collapsed_parents:
                        continue
                    is_parent = not bool(s.get("parent_key"))
                    sub_count = (
                        len(db.get_sub_stories(s["story_key"])) if is_parent else 0
                    )
                    card = StoryCard(
                        s,
                        selected=(display_idx == self.selected_index),
                        collapsed=(
                            is_parent and s["story_key"] in self._collapsed_parents
                        ),
                        sub_count=sub_count,
                    )
                    story_list.mount(card)
                    display_idx += 1
            try:
                self._render_diagnostics_panel()
            except Exception:
                pass
        else:
            for i, card in enumerate(self.query(StoryCard)):
                card.set_selected(i == self.selected_index)
            try:
                self._render_diagnostics_panel()
            except Exception:
                pass

        # Completed stories section
        completed_section = self.query_one("#completed-section")
        if completed:
            lines = ["[dim]─── Completed ───[/]"]
            for s in completed[-10:]:
                key = s["story_key"]
                title = (s.get("title") or "")[:40]
                stage = s.get("current_stage", "")
                lines.append(
                    f"  [dim green]✓[/] [dim]{key}[/]  {title}  [dim]{stage}[/]"
                )
            completed_section.update("\n".join(lines))
        else:
            completed_section.update("")

        footer = self.query_one("#footer-bar")
        footer.update(
            " [dim][n] new  [N] sub  [i] inbox  [e] enter  [s] skip  [a] abort  [f] fail  [x] delete  [r] resume  [y] copilot  [?] help[/]"
        )

    def _render_diagnostics_panel(self) -> None:
        """Render the right-side diagnostics panel for the selected story."""
        panel = self.query_one("#diagnostics-panel")
        if not self.stories or self.selected_index >= len(self.stories):
            panel.update("[dim]No story selected[/]")
            return

        s = self.stories[self.selected_index]
        key = s["story_key"]
        try:
            from ..orchestrator.debug_packet import build_debug_packet

            packet = build_debug_packet(key)
        except Exception as exc:
            panel.update(f"[red]Error building diagnostics: {exc}[/]")
            return

        if "error" in packet:
            panel.update(f"[dim]Diagnostics unavailable: {packet['error']}[/]")
            return

        story = packet["story"]
        stuck = packet["stuck_reason"]
        session = packet["session_state"]
        events = packet.get("recent_events", [])
        done = packet["done_state"]

        severity_color = {
            "error": "red",
            "warning": "yellow",
            "info": "dim",
        }.get(stuck.get("severity", "info"), "dim")

        lines = [
            "[bold]Diagnostics[/]",
            "",
            f"[bold cyan]{key}[/]",
            f"status: {story['status']}",
            f"stage: {story['current_stage']}",
            "",
        ]

        if stuck["code"] != "none":
            lines.append(f"[bold {severity_color}]可能卡住：[/]")
            lines.append(f"[{severity_color}]{stuck['message']}[/]")
        else:
            lines.append("[dim]未发现阻塞信号[/]")

        lines.append("")

        if session.get("cli_exit_state") and session["cli_exit_state"] != "none":
            lines.append(f"[dim]CLI exit: {session['cli_exit_state']}[/]")
        if session.get("session_name"):
            alive = "alive" if session.get("session_alive") else "dead"
            lines.append(f"[dim]Session: {session['session_name']} ({alive})[/]")

        if not done.get("exists"):
            lines.append("[dim]Done: missing[/]")
        elif done.get("valid") is False:
            lines.append("[red]Done: corrupted[/]")

        lines.append("")

        lines.append("[bold]最近事件：[/]")
        for ev in events[-8:]:
            et = ev.get("event_type", "?")
            ts = str(ev.get("created_at", ""))[:16]
            lines.append(f"[dim]{ts} {et}[/]")

        lines.append("")

        # Copilot section
        if self._copilot_loading:
            lines.append("[bold cyan]Copilot 思考中...[/]")
            lines.append(f"[dim]Q: {self._copilot_question}[/]")
        elif self._copilot_result:
            lines.append("[bold cyan]═══ Copilot ═══[/]")
            lines.append(f"[dim]Q: {self._copilot_question}[/]")
            lines.append("")
            if self._copilot_result.get("error"):
                lines.append(f"[red]错误: {self._copilot_result['error']}[/]")
            else:
                suggestions = self._copilot_result.get("suggestions", [])
                for i, sug in enumerate(suggestions):
                    conf = sug.get("confidence", "medium")
                    conf_color = {
                        "high": "green",
                        "medium": "yellow",
                        "low": "dim",
                    }.get(conf, "dim")
                    lines.append(f"[{conf_color}]◆ {sug['action']}[/]")
                    if sug.get("summary"):
                        lines.append(f"  [dim]{sug['summary']}[/]")
                    lines.append(f"  [dim]confidence: {conf}[/]")
                    if i < len(suggestions) - 1:
                        lines.append("")

                actions = self._copilot_result.get("actions", [])
                if actions:
                    lines.append("")
                    lines.append("[bold cyan]建议操作：[/]")
                    risk_color = {
                        "read_only": "dim",
                        "local_config": "yellow",
                        "workflow_state": "red",
                    }
                    for i, a in enumerate(actions):
                        rc = risk_color.get(a.get("risk", ""), "dim")
                        confirm = " [需确认]" if a.get("requires_confirm") else ""
                        lines.append(f"  [{i + 1}] [{rc}]{a['label']}[/]{confirm}")
                        if a.get("reason"):
                            lines.append(f"      [dim]{a['reason']}[/]")
                    lines.append(f"[dim]按 [1-{min(len(actions), 3)}] 执行操作[/]")

        lines.extend(
            [
                "",
                "[[p]] package  [[P]] global  [[y]] ask copilot",
            ]
        )

        panel.update("\n".join(lines))

    def action_cursor_up(self):
        if self.selected_index > 0:
            self.selected_index -= 1
            self._copilot_result = None
            self._copilot_loading = False
            self._render(full=False)

    def action_cursor_down(self):
        visible = self._visible_stories()
        if visible and self.selected_index < len(visible) - 1:
            self.selected_index += 1
            self._copilot_result = None
            self._copilot_loading = False
            self._render(full=False)

    def action_open_action_menu(self):
        self.action_toggle_detail()

    def action_enter_terminal(self):
        if not self.stories:
            _tui_debug("enter_terminal_no_stories")
            return
        s = self.stories[self.selected_index]
        story_key = s["story_key"]
        session = ttyd.session_name(story_key)

        from ..orchestrator.graph import is_story_running, is_workspace_locked

        is_running = is_story_running(story_key)
        ws = s.get("workspace", "")
        ws_state = (
            WorkspaceState.LOCKED_BY_OTHER
            if ws and is_workspace_locked(ws, exclude_story=story_key)
            else WorkspaceState.FREE
        )
        state = resolve_stage_state(
            s, self._session_backend, is_running, workspace_state=ws_state
        )
        action = decide_action(state, "e")
        _tui_debug(
            "enter_terminal_decision",
            story_key=story_key,
            state=state.value,
            action=action.value,
        )

        if action == StageEntryAction.ATTACH:
            attach_args = self._session_backend.attach_foreground(session)
            if os.name == "nt":
                self._pending_attach_args = attach_args
                self.exit()
                return
            try:
                with self.suspend():
                    subprocess.run(attach_args, check=False)
            except Exception:
                self.exit()
                subprocess.run(attach_args, check=False)
                os.system("story board")
        else:
            notice = entry_action_notice(action, s)
            if notice:
                severity = (
                    "error"
                    if action
                    in (
                        StageEntryAction.PROMPT_FIX_DONE,
                        StageEntryAction.SHOW_CLI_EXIT_ERROR,
                        StageEntryAction.SHOW_SESSION_UNKNOWN,
                    )
                    else "warning"
                )
                self.notify(notice, severity=severity)
            panel = self.query_one("#detail-panel")
            panel.update(f"[bold yellow]{notice or '不可操作'}[/]")
            panel.set_class(True, "visible")
            self._show_detail = True

    def action_toggle_detail(self):
        self._show_detail = not self._show_detail
        panel = self.query_one("#detail-panel")
        if self._show_detail and self.stories:
            s = self.stories[self.selected_index]
            panel.update(_render_detail(s))
            panel.set_class(True, "visible")
        else:
            panel.set_class(False, "visible")

    def action_new_story(self):
        def on_result(result):
            if result is None:
                return
            key, title, prd_path = result
            try:
                create_and_start_story(
                    story_key=key,
                    title=title,
                    workspace=os.getcwd(),
                    prd_path=prd_path or None,
                )
                self.refresh_stories()

                # Show initial plan panel (timer takes over rotation)
                self._plan_story_key = key
                self._spinner_idx = 0
                self._plan_label = "正在规划中..."
                self._plan_start_time = time.time()
                panel = self.query_one("#plan-panel")
                panel.update(
                    f"[bold]{key}[/]  [dim]design  │[/]  "
                    f"[bold cyan]⠉⠁[/] {self._plan_label}  [dim]0s[/]"
                )
                panel.set_class(True, "visible")

                # Start the graph — handles plan → execute → poll → review → advance
                from ..orchestrator.graph import start_story_async

                start_story_async(key)
            except Exception as e:
                panel = self.query_one("#detail-panel")
                panel.update(f"[red]Failed to create story: {e}[/]")
                panel.set_class(True, "visible")
                self._show_detail = True

        self.push_screen(NewStoryDialog(), on_result)

    def _start_stage(self, story_key: str):
        """Launch AI CLI for the current stage. Runs in background worker."""
        import json

        from ..adapters import get_adapter
        from ..orchestrator.nodes import get_stage_config, _render_prompt

        s = db.get_story(story_key)
        if not s:
            return
        stage = s["current_stage"]
        workspace = s["workspace"]
        profile = s.get("profile", "minimal")

        try:
            cfg = get_stage_config(profile, stage)
            profile_data = load_profile(profile)
            adapter_name = cfg.get("cli", profile_data.get("cli", "claude"))
            model = cfg.get("model", "sonnet")
            adapter = get_adapter(adapter_name)

            # Load context from DB (includes prd_path)
            try:
                ctx = json.loads(s.get("context_json") or "{}")
            except (json.JSONDecodeError, TypeError):
                ctx = {}

            state = {
                "story_key": story_key,
                "title": s.get("title", ""),
                "workspace": workspace,
                "profile": profile,
                "current_stage": stage,
                "context": ctx,
            }
            prompt, _ = _render_prompt(stage, state)
            launch = adapter.launch_cmd(model)

            # Always launch in a new terminal window — avoids TUI crash from
            # calling suspend() in a worker thread.
            tmp = Path(tempfile.gettempdir()) / f"story-prompt-{story_key}-{stage}.md"
            tmp.write_text(prompt, encoding="utf-8")
            db.log_stage(story_key, stage, "execute", "Launched in new window")
            ttyd.launch_cli(story_key, workspace, launch, str(tmp))
        except Exception as e:
            self.call_from_thread(
                self._show_error,
                f"[yellow]Story created but execution failed: {e}[/]\n\n"
                "  Press [[e]] to enter the session manually.",
            )

    def _show_error(self, message: str):
        """Show error in detail panel (safe to call from any thread)."""
        panel = self.query_one("#detail-panel")
        panel.update(message)
        panel.set_class(True, "visible")
        self._show_detail = True

    # ---- plan panel rendering ----

    def _elapsed_str(self) -> str:
        elapsed = int(time.time() - self._plan_start_time)
        if elapsed < 60:
            return f"{elapsed}s"
        return f"{elapsed // 60}m{elapsed % 60:02d}s"

    def _update_plan_panel(self) -> None:
        """Re-render the plan panel with current spinner frame, label and timer."""
        if self._spinner_idx < 0:
            return
        spinner = self._SPINNER_FRAMES[self._spinner_idx % len(self._SPINNER_FRAMES)]
        label = self._plan_label
        elapsed = self._elapsed_str()
        panel = self.query_one("#plan-panel")
        panel.update(
            f"[bold]{self._plan_story_key}[/]  [dim]design  │[/]  "
            f"[bold cyan]{spinner}[/] {label}  [dim]{elapsed}[/]"
        )

    async def tick_spinner(self) -> None:
        """Rotate spinner and poll in-memory status bus."""
        try:
            # Check for foreground terminal requests (fast response)
            from ..orchestrator.graph import take_terminal_request

            for s in self.stories:
                args = take_terminal_request(s["story_key"])
                if args:
                    self._pending_attach_args = args
                    self.exit()
                    return

            if self._spinner_idx < 0:
                return
            result = take_plan_done(self._plan_story_key)
            if result:
                summary, ok = result
                self._plan_label = summary[:60] if ok else f"⚠ {summary[:60]}"
            if take_terminal_opened(self._plan_story_key):
                self._plan_label = "✓ 终端已启动"
            self._spinner_idx += 1
            self._update_plan_panel()
            panel = self.query_one("#plan-panel")
            panel.set_class(True, "visible")
        except Exception:
            pass  # never let a tick crash kill the interval

    def action_skip_stage(self):
        if not self.stories:
            return
        s = self.stories[self.selected_index]
        key = s["story_key"]

        from ..orchestrator.graph import is_story_running

        if is_story_running(key):
            self.notify(
                f"Story {key} 的 graph 正在运行，不能跳过。请先等待完成或 abort。",
                severity="warning",
            )
            return

        skip_stage(key, s["current_stage"])
        self.refresh_stories()

    def action_fail_story(self):
        if not self.stories:
            return
        s = self.stories[self.selected_index]
        key = s["story_key"]

        from ..orchestrator.graph import is_story_running

        if is_story_running(key):
            self.notify(
                f"Story {key} 的 graph 正在运行。如需标记失败，请先按 [a] abort。",
                severity="warning",
            )
            return

        fail_story(key)
        self.refresh_stories()

    def action_accept_risk_advance(self):
        if not self.stories:
            return
        s = self.stories[self.selected_index]
        key = s["story_key"]

        from ..orchestrator.graph import is_story_running

        if is_story_running(key):
            self.notify(
                f"Story {key} 的 graph 正在运行。请等待完成。",
                severity="warning",
            )
            return

        def on_confirm(confirmed):
            if not confirmed:
                return
            db.log_event(
                key,
                s.get("current_stage", ""),
                "human_gate_action",
                {
                    "action": "accept_risk_advance",
                    "actor": "local_user",
                    "reason": "Manually accepted risk via TUI",
                },
            )
            db.update_story(key, status="active", last_error=None)
            from ..orchestrator.graph import start_story_async

            start_story_async(key)
            self.refresh_stories()

        self.push_screen(
            ConfirmDialog(
                f"Accept risk and advance {key}?\n\n"
                f"This will skip the review gate and proceed to the next stage.\n"
                f"The action is audited but cannot guarantee quality."
            ),
            on_confirm,
        )

    def action_delete_story(self):
        if not self.stories:
            return
        s = self.stories[self.selected_index]
        key = s["story_key"]
        session = ttyd.session_name(key)

        from ..orchestrator.graph import is_story_running

        is_running = is_story_running(key)
        warning = ""
        if is_running:
            warning = "\n\n[bold red]Story graph 正在运行，删除将中止执行。[/]"

        def on_confirm(confirmed):
            if not confirmed:
                return
            _tui_debug("delete_story", story_key=key)
            if is_running:
                from ..orchestrator.graph import force_stop_story

                force_stop_story(key)
            ttyd.kill_session(session)
            ttyd.stop_ttyd(key)
            delete_story(key)
            marker = cli_exit_marker_path(key)
            if marker.exists():
                marker.unlink()
            self.refresh_stories()

        self.push_screen(
            ConfirmDialog(f"Delete story {key}?{warning}"),
            on_confirm,
        )

    def action_resume_story(self):
        if not self.stories:
            return
        try:
            self._resume_story_impl()
        except Exception as exc:
            self.notify(f"Resume failed: {exc}", severity="error")

    def _resume_story_impl(self):
        s = self.stories[self.selected_index]
        key = s["story_key"]
        session = ttyd.session_name(key)

        from ..orchestrator.graph import (
            is_story_running,
            start_story_async,
            is_workspace_locked,
        )

        is_running = is_story_running(key)
        ws = s.get("workspace", "")
        ws_state = (
            WorkspaceState.LOCKED_BY_OTHER
            if ws and is_workspace_locked(ws, exclude_story=key)
            else WorkspaceState.FREE
        )
        state = resolve_stage_state(
            s, self._session_backend, is_running, workspace_state=ws_state
        )
        action = decide_action(state, "r")
        _tui_debug(
            "resume_story_decision",
            story_key=key,
            state=state.value,
            action=action.value,
        )

        # Gate-specific resume: retry review only, skip executor
        if state == StageEntryState.GATE_WAIT_CONFIRM:
            if action == StageEntryAction.RETRY_REVIEW:
                db.update_story(key, status="active", last_error=None)
                db.update_context(key, "last_gate_decision_id", "")
                db.update_context(key, "last_gate_decision", "")
                if not is_story_running(key):
                    start_story_async(key)
                self.refresh_stories()
                self.notify(f"Retrying review for {key}...")
                return
            elif action == StageEntryAction.SHOW_GATE_STATUS:
                self.action_toggle_detail()
                return

        if action == StageEntryAction.START_OR_RESUME:
            if state == StageEntryState.IDLE_WITH_LIVE_SESSION:
                _tui_debug("cleanup_live_before_start", story_key=key)
                ttyd.kill_session(session)
            db.update_story(key, status="active", last_error=None)
            if not is_story_running(key):
                start_story_async(key)
            self.refresh_stories()

        elif action == StageEntryAction.CONSUME_DONE_RESUME:
            db.update_story(key, status="active", last_error=None)
            if not is_story_running(key):
                start_story_async(key)
            self.refresh_stories()

        elif action == StageEntryAction.CLEANUP_DEAD_AND_START:
            _tui_debug("cleanup_dead_and_start", story_key=key)
            ttyd.delete_exited_session(session)
            marker = cli_exit_marker_path(key)
            if marker.exists():
                marker.unlink()
            db.update_story(key, status="active", last_error=None)
            if not is_story_running(key):
                start_story_async(key)
            self.refresh_stories()

        elif action == StageEntryAction.CLEANUP_DEAD_AND_RESTART:

            def on_restart_confirm(confirmed):
                if not confirmed:
                    return
                _tui_debug("cleanup_dead_and_restart", story_key=key)
                from ..orchestrator.graph import force_stop_story

                force_stop_story(key)
                ttyd.delete_exited_session(session)
                marker = cli_exit_marker_path(key)
                if marker.exists():
                    marker.unlink()
                db.update_story(key, status="active", last_error=None)
                start_story_async(key)
                self.refresh_stories()

            self.push_screen(
                ConfirmDialog(
                    f"Story {key} 的 graph 仍在运行但 session 已退出。\n"
                    f"强制重启将中止当前后台执行。是否继续？"
                ),
                on_restart_confirm,
            )

        else:
            notice = entry_action_notice(action, s)
            if notice:
                severity = (
                    "error" if action == StageEntryAction.PROMPT_FIX_DONE else "warning"
                )
                self.notify(notice, severity=severity)
            panel = self.query_one("#detail-panel")
            panel.update(f"[bold yellow]{notice or '不可操作'}[/]")
            panel.set_class(True, "visible")
            self._show_detail = True

    def action_new_sub_story(self):
        if not self.stories:
            return
        s = self.stories[self.selected_index]
        key = s["story_key"]

        def on_result(result):
            if result is None:
                return
            try:
                from ..orchestrator.service import create_sub_story

                sub_key = create_sub_story(
                    parent_key=key,
                    sub_type=result.get("sub_type") or None,
                    start_stage=result.get("start_stage") or None,
                    description=result["description"],
                )
                self.refresh_stories()
                panel = self.query_one("#detail-panel")
                panel.update(f"[green]Created sub-story: {sub_key}[/]")
                panel.set_class(True, "visible")
                self._show_detail = True
            except Exception as e:
                panel = self.query_one("#detail-panel")
                panel.update(f"[red]Failed to create sub-story: {e}[/]")
                panel.set_class(True, "visible")
                self._show_detail = True

        self.push_screen(SubStoryDialog(key), on_result)

    def action_abort_story(self):
        if not self.stories:
            return
        s = self.stories[self.selected_index]
        key = s["story_key"]
        session = ttyd.session_name(key)

        from ..orchestrator.service import abort_story

        try:
            abort_story(key)
            if ttyd.session_alive(session):
                ttyd.kill_session(session)
            ttyd.stop_ttyd(key)
        except ValueError as e:
            panel = self.query_one("#detail-panel")
            panel.update(f"[red]{e}[/]")
            panel.set_class(True, "visible")
            self._show_detail = True
        self.refresh_stories()

    def action_toggle_collapse(self):
        if not self.stories:
            return
        visible = self._visible_stories()
        if self.selected_index >= len(visible):
            return
        s = visible[self.selected_index]
        key = s["story_key"]
        has_children = any(st.get("parent_key") == key for st in self.stories)
        if has_children:
            if key in self._collapsed_parents:
                self._collapsed_parents.discard(key)
            else:
                self._collapsed_parents.add(key)
            self._render()

    def action_refresh(self):
        self.refresh_stories()

    def _startup_sweep(self):
        """On startup, check all non-terminal stories for existing done files and resume."""
        from ..orchestrator.graph import start_story_async, is_story_running

        for s in self.stories:
            if s["status"] in _FINISHED_STATUSES:
                continue
            key = s["story_key"]
            if has_stage_done(s):
                validation = validate_stage_done(s)
                if validation.status == DoneStatus.OK and not is_story_running(key):
                    db.update_story(key, status="active", last_error=None)
                    start_story_async(key)

    async def watchdog_check(self):
        from ..orchestrator.graph import (
            resume_story,
            is_story_running,
            take_terminal_request,
        )
        from ..db import models as db

        # Check for foreground terminal execution requests
        for s in self.stories:
            key = s["story_key"]
            args = take_terminal_request(key)
            if args:
                _tui_debug(
                    "watchdog_terminal_request",
                    story_key=key,
                    args=args,
                )
                self._pending_attach_args = args
                self.exit()
                return

        active = [s for s in self.stories if s["status"] in ("active", "paused")]
        for s in active:
            key = s["story_key"]

            if has_stage_done(s) and not is_story_running(key):
                validation = validate_stage_done(s)
                if validation.status == DoneStatus.OK:
                    db.update_story(key, status="active")
                    try:
                        resume_story(key)
                    except Exception:
                        pass
            elif not has_stage_done(s) and not is_story_running(key):
                cli_state = resolve_cli_exit_state(s)
                if cli_state == CliExitState.EXITED_WITHOUT_DONE:
                    _tui_debug(
                        "watchdog_cli_exit_detected",
                        story_key=key,
                    )

        # Unblock sub-stories whose dependencies are complete
        from ..orchestrator.graph import start_story_async

        blocked_stories = [
            s for s in self.stories if s["status"] == "blocked" and s.get("parent_key")
        ]
        for story in blocked_stories:
            parent_key = story["parent_key"]
            siblings = db.get_sub_stories(parent_key)
            completed_keys = {
                c["story_key"] for c in siblings if c["status"] in ("completed",)
            }
            # Check deps from delegate event
            events = db.get_story_events(parent_key)
            deps = []
            for ev in events:
                if ev["event_type"] == "delegate" and ev.get("payload"):
                    import json as _json

                    payload = (
                        _json.loads(ev["payload"])
                        if isinstance(ev["payload"], str)
                        else ev["payload"]
                    )
                    if payload.get("sub_key") == story["story_key"]:
                        deps = payload.get("depends_on", [])
                        break
            required = {f"{parent_key}-{d}" for d in deps}
            if required and required.issubset(completed_keys):
                db.update_story(story["story_key"], status="active")
                db.log_event(
                    story["story_key"], "", "unblocked", {"deps_met": list(required)}
                )
                start_story_async(story["story_key"])

        # Check for parents waiting on subtasks
        TERMINAL_STATES = ("completed", "failed", "blocked")
        pending_parents = db.get_pending_parents()
        for parent in pending_parents:
            children = db.get_sub_stories(parent["story_key"])
            incomplete = [c for c in children if c["status"] not in TERMINAL_STATES]
            if not incomplete:
                # Atomic status transition to prevent double-resume
                conn = db.get_conn()
                updated = conn.execute(
                    "UPDATE story SET status = 'active' WHERE story_key = ? AND status = 'waiting_subtasks'",
                    (parent["story_key"],),
                ).rowcount
                conn.commit()
                conn.close()
                if updated:
                    db.log_event(
                        parent["story_key"],
                        "",
                        "subtasks_completed",
                        {
                            "children": [c["story_key"] for c in children],
                        },
                    )
                    try:
                        resume_story(parent["story_key"])
                    except Exception:
                        pass

        new_interval = 3 if active else 30
        if new_interval != self._watchdog_interval:
            self._watchdog_interval = new_interval

    def action_quit(self):
        _tui_debug("quit_tui")
        self.exit()

    def action_run_doctor(self):
        """Show doctor results in detail panel."""
        import io
        from .doctor import run_doctor
        from rich.console import Console

        buf = io.StringIO()
        doc_console = Console(file=buf, force_terminal=True)
        # Temporarily redirect doctor output
        import story_lifecycle.cli.doctor as doc_mod

        orig = doc_mod.console
        doc_mod.console = doc_console
        try:
            run_doctor()
        finally:
            doc_mod.console = orig

        panel = self.query_one("#detail-panel")
        panel.update(buf.getvalue())
        panel.set_class(True, "visible")
        self._show_detail = True

    def action_run_setup(self):
        """Re-run setup wizard in a suspended terminal."""
        from .setup import run_setup, load_config_to_env

        if os.name == "nt":
            import subprocess

            subprocess.Popen(
                ["python", "-m", "story_lifecycle.cli.setup"],
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
        else:
            with self.suspend():
                run_setup()
        load_config_to_env()
        self.refresh_stories()

    # ---- source polling ----

    def _poll_source(self) -> None:
        """Trigger background poll using Textual worker."""
        if not self._source_enabled:
            return
        self.run_worker(self._do_poll, thread=True, exclusive=True, group="source_poll")

    def _do_poll(self) -> None:
        """Background thread: fetch pending items from source."""
        from ..sources import get_source
        from ..cli.setup import get_config
        from ..db import models as db

        try:
            config = get_config()
            source_name = config.get("story_source", {}).get("enabled", "")
            source = get_source(source_name)
            if not source:
                return
            items = source.fetch_pending()
            new_items = [i for i in items if not db.find_by_source_id(i.source, i.id)]
            if new_items:
                self._pending_items = new_items
                self.call_from_thread(self._update_inbox_notification, len(new_items))
        except Exception:
            pass

    def _update_inbox_notification(self, count: int):
        """Update header with inbox notification count."""
        try:
            header = self.query_one("#header-bar")
            if header:
                header.update(
                    f"\n  [bold cyan]◆[/] [bold white]Story[/][bold cyan]Lifecycle[/] "
                    f" [dim]│[/] [bold yellow]{count} 个新待办[/] "
                    f"[dim]│[/] 按 [[i]] 查看"
                )
        except Exception:
            pass

    def action_show_inbox(self):
        from ..sources import get_source
        from .setup import get_config

        config = get_config()
        source_name = config.get("story_source", {}).get("enabled", "")
        if not source_name:
            self.notify("未配置外部来源，请运行 story setup", severity="warning")
            return

        source = get_source(source_name)
        if not source:
            self.notify(f"来源 {source_name} 不可用", severity="error")
            return

        loading = LoadingScreen()
        self.push_screen(loading)

        def _do_fetch():
            try:
                items = source.fetch_pending()
                items = [i for i in items if not db.find_by_source_id(i.source, i.id)]
            except Exception as e:
                self.call_from_thread(loading.dismiss)
                self.call_from_thread(
                    self.notify, f"获取待办失败: {e}", severity="error"
                )
                return
            self.call_from_thread(loading.dismiss)
            if not items:
                self.call_from_thread(self.notify, "没有新的待办")
                return
            screen = InboxScreen(items)
            self.call_from_thread(self.push_screen, screen, self._on_inbox_result)

        import threading

        t = threading.Thread(target=_do_fetch, daemon=True)
        t.start()

    def _on_inbox_result(self, result):
        if not result:
            return
        from ..orchestrator.service import create_story_from_source

        for entry in result:
            try:
                if isinstance(entry, tuple) and len(entry) == 2:
                    mode, item = entry
                else:
                    mode, item = "normal", entry
                use_ai_prd = mode == "ai_prd"
                r = create_story_from_source(
                    item, auto_start=True, generate_ai_prd=use_ai_prd
                )
                if r.status == "created":
                    label = "AI增强PRD" if use_ai_prd else "已创建"
                    self.notify(f"{label}: {r.story_key}")
                elif r.status == "need_manual_select":
                    from ..orchestrator.service import create_sub_story

                    active = [s for s in self.stories if not s.get("parent_key")]

                    def _on_parent_selected(parent_key):
                        if parent_key == "":
                            return  # Cancel
                        if parent_key is None:
                            # Standalone
                            r2 = create_story_from_source(
                                item,
                                auto_start=True,
                                generate_ai_prd=use_ai_prd,
                                force_standalone=True,
                            )
                            if r2.status == "created":
                                self.notify(f"已创建独立故事: {r2.story_key}")
                        else:
                            from ..sources.bug_providers import (
                                fetch_bug_content,
                                format_bug_context,
                            )
                            from ..orchestrator.graph import start_story_async
                            from ..db import models as db

                            bug_ctx = fetch_bug_content(item)
                            bug_desc = format_bug_context(bug_ctx)
                            sub_key = create_sub_story(
                                parent_key=parent_key,
                                sub_type="bug-fix",
                                description=bug_desc,
                            )
                            if sub_key:
                                db.update_story(
                                    sub_key,
                                    source_type=item.source,
                                    source_id=item.id,
                                )
                                start_story_async(sub_key)
                                self.notify(f"已创建子故事: {sub_key}")

                    self.push_screen(
                        ParentSelectDialog(item.title, active), _on_parent_selected
                    )
                else:
                    self.notify(f"创建失败: {r.error}", severity="error")
            except Exception as e:
                self.notify(f"创建失败: {e}", severity="error")
        self.refresh_stories()

    def action_help(self):
        self._show_detail = True
        panel = self.query_one("#detail-panel")
        panel.update(
            "[bold]Key Bindings[/]\n"
            "  ↑/k     Move up\n"
            "  ↓/j     Move down\n"
            "  Enter   Toggle detail\n"
            "  n       New story\n"
            "  e       Enter terminal\n"
            "  d       Toggle detail\n"
            "  s       Skip stage\n"
            "  f       Mark failed\n"
            "  x       Delete story\n"
            "  r       Resume\n"
            "  R/F5    Refresh\n"
            "  D       Doctor (env check)\n"
            "  S       Setup (reconfigure)\n"
            "  i       Inbox (external source)\n"
            "  y       Ask Copilot (diagnostics)\n"
            "  ?       Help\n"
            "  q       Quit"
        )
        panel.set_class(True, "visible")

    def action_toggle_diagnostics(self):
        """Toggle the right-side diagnostics panel visibility."""
        self._show_diagnostics = not self._show_diagnostics
        panel = self.query_one("#diagnostics-panel")
        panel.set_class(not self._show_diagnostics, "hidden")
        if self._show_diagnostics:
            self._render_diagnostics_panel()

    def action_package_story_diagnostics(self):
        """Generate a diagnostic bundle for the selected story."""
        if not self.stories or self.selected_index >= len(self.stories):
            self.notify("No story selected", severity="warning")
            return
        s = self.stories[self.selected_index]
        key = s["story_key"]
        try:
            from ..orchestrator.diagnostics import create_story_diagnostics_bundle

            result = create_story_diagnostics_bundle(story_key=key)
            if result.get("error"):
                self.notify(f"Diagnostics failed: {result['error']}", severity="error")
                return
            path = result["path"]
            self.notify(f"Bundle: {path}", title="Diagnostics")
            db.log_event(
                key,
                s.get("current_stage", ""),
                "diagnostic_bundle_created",
                {"bundle_path": path, "bundle_type": "story"},
            )
        except Exception as exc:
            self.notify(f"Error: {exc}", severity="error")

    def action_package_global_diagnostics(self):
        """Generate a global diagnostics bundle."""
        try:
            from ..orchestrator.diagnostics import create_global_diagnostics_bundle

            result = create_global_diagnostics_bundle()
            if result.get("error"):
                self.notify(f"Diagnostics failed: {result['error']}", severity="error")
                return
            path = result["path"]
            self.notify(f"Global bundle: {path}", title="Diagnostics")
        except Exception as exc:
            self.notify(f"Error: {exc}", severity="error")

    def action_ask_copilot(self):
        """Open the Ask Copilot dialog for the selected story."""
        if not self.stories or self.selected_index >= len(self.stories):
            self.notify("No story selected", severity="warning")
            return
        self.push_screen(CopilotDialog(), self._on_copilot_question)

    def _on_copilot_question(self, question: str | None):
        """Callback when user submits a copilot question."""
        if not question:
            return
        s = self.stories[self.selected_index]
        key = s["story_key"]
        self._copilot_loading = True
        self._copilot_question = question
        self._copilot_result = None
        self._render_diagnostics_panel()
        self.notify(f"Asking Copilot about {key}...", title="Copilot")
        self.run_worker(
            self._do_copilot_query(key, question),
            thread=True,
            exclusive=False,
        )

    def _do_copilot_query(self, story_key: str, question: str):
        """Run copilot LLM call in worker thread."""
        from ..orchestrator.copilot import ask_copilot
        from ..db import models as db

        try:
            result = ask_copilot(story_key, question)
        except Exception as exc:
            result = {
                "error": str(exc),
                "suggestions": [],
                "questions": [],
            }

        try:
            s = db.get_story(story_key)
            stage = s.get("current_stage", "") if s else ""
        except Exception:
            stage = ""

        db.log_event(
            story_key,
            stage,
            "copilot_query",
            {
                "question": question,
                "has_error": bool(result.get("error")),
                "suggestion_count": len(result.get("suggestions", [])),
            },
        )

        self.call_from_thread(self._on_copilot_done, result)

    def _on_copilot_done(self, result: dict):
        """Called on main thread when copilot query completes."""
        self._copilot_loading = False
        self._copilot_result = result
        self._render_diagnostics_panel()
        count = len(result.get("suggestions", []))
        n_actions = len(result.get("actions", []))
        if result.get("error"):
            self.notify(
                f"Copilot: {result['error']}", severity="warning", title="Copilot"
            )
        elif n_actions:
            self.notify(
                f"{count} suggestion(s), {n_actions} action(s) — press 1-{n_actions} to execute",
                title="Copilot",
            )
        else:
            self.notify(f"Copilot returned {count} suggestion(s)", title="Copilot")

    # ---- copilot action execution (P2) ----

    def action_copilot_action_1(self):
        self._execute_copilot_action(0)

    def action_copilot_action_2(self):
        self._execute_copilot_action(1)

    def action_copilot_action_3(self):
        self._execute_copilot_action(2)

    def _execute_copilot_action(self, index: int):
        """Execute a copilot-suggested action, with confirmation for risky actions."""
        if not self._copilot_result:
            return
        actions = self._copilot_result.get("actions", [])
        if index >= len(actions):
            return
        a = actions[index]

        if a.get("requires_confirm"):
            risk_label = {
                "workflow_state": "工作流状态变更",
                "local_config": "本地配置修改",
            }.get(a.get("risk", ""), a.get("risk", ""))

            def on_confirm(confirmed):
                if confirmed:
                    self._do_execute_copilot_action(a)
                else:
                    self._log_copilot_action(a, "rejected")

            self.push_screen(
                ConfirmDialog(
                    f"执行操作: {a['label']}?\n\n"
                    f"操作: {a['action']}\n"
                    f"风险等级: {risk_label}\n"
                    f"原因: {a.get('reason', '')}\n\n"
                    f"该操作将修改工作流状态，是否继续？"
                ),
                on_confirm,
            )
        else:
            self._do_execute_copilot_action(a)

    def _do_execute_copilot_action(self, action: dict):
        """Execute a copilot action and log it to event_log."""
        self._log_copilot_action(action, "confirmed")

        handlers = {
            "package_diagnostics": self.action_package_story_diagnostics,
            "run_doctor": self.action_run_doctor,
            "enter_terminal": self.action_enter_terminal,
            "run_setup": self.action_run_setup,
            "resume_story": self.action_resume_story,
            "skip_stage": self.action_skip_stage,
            "fail_story": self.action_fail_story,
            "abort_story": self.action_abort_story,
        }
        handler = handlers.get(action["action"])
        if handler:
            handler()

    def _log_copilot_action(self, action: dict, outcome: str):
        """Log copilot action confirmation/rejection to event_log."""
        if not self.stories or self.selected_index >= len(self.stories):
            return
        s = self.stories[self.selected_index]
        db.log_event(
            s["story_key"],
            s.get("current_stage", ""),
            f"copilot_action_{outcome}",
            {
                "action": action["action"],
                "label": action["label"],
                "risk": action["risk"],
            },
        )

    def on_resize(self, event):
        """Handle terminal resize -- hide diagnostics on narrow screens."""
        width = event.size.width
        panel = self.query_one("#diagnostics-panel")
        if width < 120:
            if self._show_diagnostics:
                self._show_diagnostics = False
                panel.set_class(True, "hidden")
        else:
            if not self._show_diagnostics:
                self._show_diagnostics = True
                panel.set_class(False, "hidden")
                self._render_diagnostics_panel()


def run_tui():
    """Entry point for the TUI board."""
    _tui_debug("run_tui_start", os_name=os.name)
    while True:
        app = StoryBoardApp()
        _tui_debug("run_tui_app_start")
        app.run()
        attach_args = app._pending_attach_args
        _tui_debug("run_tui_app_return", attach_args=attach_args)
        if os.name != "nt" or not attach_args:
            _tui_debug("run_tui_exit", os_name=os.name, attach_args=attach_args)
            break
        try:
            _tui_debug("run_tui_attach_start", args=attach_args)
            if (
                len(attach_args) >= 4
                and attach_args[0] == "zellij"
                and "--session" in attach_args
                and "--new-session-with-layout" in attach_args
            ):
                session_name = attach_args[attach_args.index("--session") + 1]
                deleted = ttyd.delete_exited_session(session_name)
                _tui_debug(
                    "run_tui_zellij_delete_exited",
                    session_name=session_name,
                    deleted=deleted,
                )
            _prepare_terminal_for_child()
            _tui_debug(
                "run_tui_attach_stdio",
                stdin_isatty=sys.__stdin__.isatty(),
                stdout_isatty=sys.__stdout__.isatty(),
                stderr_isatty=sys.__stderr__.isatty(),
                stdin_name=getattr(sys.__stdin__, "name", None),
                stdout_name=getattr(sys.__stdout__, "name", None),
                term=os.environ.get("TERM"),
                wt_session=os.environ.get("WT_SESSION"),
            )
            result = subprocess.run(
                attach_args,
                check=False,
                stdin=sys.__stdin__,
                stdout=sys.__stdout__,
                stderr=sys.__stderr__,
            )
            _tui_debug("run_tui_attach_return", returncode=result.returncode)

            if result.returncode != 0:
                sys.__stdout__.write(
                    f"\n\x1b[31mZellij/terminal command failed (exit code: {result.returncode})\x1b[0m\n"
                    f"Command: {' '.join(attach_args)}\n"
                    f"Press Enter to return to TUI...\n"
                )
                sys.__stdout__.flush()
                try:
                    input()
                except EOFError:
                    pass

            if len(attach_args) >= 3 and "--session" in attach_args:
                from ..orchestrator.graph import emit_terminal_opened

                session_name = attach_args[attach_args.index("--session") + 1]
                if session_name.startswith("s-"):
                    emit_terminal_opened(session_name[2:])
        except Exception as exc:
            _tui_debug(
                "run_tui_attach_exception",
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise
