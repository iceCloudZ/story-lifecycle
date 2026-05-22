"""Interactive TUI board — Textual App for story management."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Footer, Static, Input, Button
from textual.reactive import reactive

from ..db import models as db
from ..orchestrator.graph import set_tui_app
from ..orchestrator.service import (
    get_story_cli_model,
    create_and_start_story,
)
from ..orchestrator.nodes import load_profile
from ..terminal import ttyd


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

    def __init__(self, story: dict, selected: bool = False):
        self.story = story
        self._selected = selected
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

        badge = {
            "active": "[bold green]> active[/]",
            "paused": "[bold yellow]|| paused[/]",
            "blocked": "[bold red]X blocked[/]",
            "completed": "[dim green]OK done[/]",
            "waiting_subtasks": "[bold magenta]≡ waiting subs[/]",
        }.get(status, status)

        try:
            profile = load_profile(profile_name)
            stages = list(profile.get("stages", {}).keys())
            bar = _render_stage_bar(stages, stage)
        except FileNotFoundError:
            bar = stage

        cli_info = get_story_cli_model(key)
        cli_line = f"  [dim]CLI: {cli_info['cli']} · Model: {cli_info['model']}[/]"

        cursor = "[bold cyan]▸[/] " if self._selected else "  "
        indent = "  └─ " if is_child else ""

        lines = [
            f"{cursor}{indent}[bold cyan]{key}[/]  {title}",
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


class StoryBoardApp(App):
    """Interactive story board TUI."""

    CSS = """
    Screen {
        layout: vertical;
        background: $surface;
    }

    #header-bar {
        height: auto;
        min-height: 3;
        padding: 1 2;
        background: $boost;
        border-bottom: solid $accent;
    }

    #plan-panel {
        height: 0;
        padding: 0;
        display: none;
    }
    #plan-panel.visible {
        height: auto;
        max-height: 6;
        padding: 1 2;
        background: $panel;
        border-bottom: solid $accent;
        display: block;
        overflow-y: auto;
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
        Binding("shift+d", "run_doctor", "Doctor", key_display="D"),
        Binding("shift+s", "run_setup", "Setup", key_display="S"),
        Binding("question_mark", "help", "Help", key_display="?"),
        Binding("q", "quit", "Quit"),
    ]

    selected_index: reactive[int] = reactive(0)
    stories: reactive[list[dict]] = reactive([])

    def compose(self) -> ComposeResult:
        yield Static(id="header-bar")
        yield Static(id="plan-panel")
        yield VerticalScroll(id="story-list")
        yield Static(id="detail-panel")
        yield Static(id="footer-bar")
        yield Footer()

    def on_mount(self):
        set_tui_app(self)
        self._watchdog_interval = 3
        self._show_detail = False
        self._plan_story_key = ""
        self._spinner_idx = -1  # -1 = stopped
        self.refresh_stories()
        self.set_interval(5, self.refresh_stories)
        self.set_interval(3, self.watchdog_check)
        self.set_interval(0.15, self.tick_spinner)

    def refresh_stories(self):
        self.stories = db.list_active_stories()
        self._render()

    def _render(self, full: bool = True):
        from ..orchestrator.router import llm_is_available
        from .setup import get_config

        config = get_config()
        provider = config.get("provider", "N/A")
        router_status = f"enabled ({provider})" if llm_is_available() else "disabled"
        active = len([s for s in self.stories if s["status"] == "active"])

        header = self.query_one("#header-bar")
        header.update(
            "\n"
            "  [bold cyan]◆[/] [bold white]Story[/][bold cyan]Lifecycle[/] "
            f" [dim]│[/] Router: {router_status} [dim]│[/] Stories: {active} active"
        )

        if full:
            story_list = self.query_one("#story-list")
            story_list.remove_children()
            if not self.stories:
                story_list.mount(
                    Static("[dim]No active stories. Press [[n]] to create one.[/]")
                )
            else:
                for i, s in enumerate(self.stories):
                    card = StoryCard(s, selected=(i == self.selected_index))
                    story_list.mount(card)
        else:
            for i, card in enumerate(self.query(StoryCard)):
                card.set_selected(i == self.selected_index)

        footer = self.query_one("#footer-bar")
        footer.update(
            " [dim][n] new  [e] enter  [s] skip  [f] fail  [x] delete  [r] resume  [D] doctor  [S] setup  [?] help[/]"
        )

    def action_cursor_up(self):
        if self.selected_index > 0:
            self.selected_index -= 1
            self._render(full=False)

    def action_cursor_down(self):
        if self.stories and self.selected_index < len(self.stories) - 1:
            self.selected_index += 1
            self._render(full=False)

    def action_open_action_menu(self):
        self.action_toggle_detail()

    def action_enter_terminal(self):
        if not self.stories:
            return
        s = self.stories[self.selected_index]
        session = ttyd.session_name(s["story_key"])
        workspace = s.get("workspace", os.getcwd())

        # Try to create the session on-the-fly if it doesn't exist
        if not ttyd.session_alive(session):
            ttyd.create_session(session, workspace)

        if not ttyd.session_alive(session):
            # No multiplexer available — launch AI CLI directly
            self._launch_cli_direct(s, workspace)
            return

        attach = ttyd.attach_cmd(session)
        try:
            with self.suspend():
                os.system(attach)
        except Exception:
            self.exit()
            os.system(attach)
            os.system("story board")

    def _launch_cli_direct(self, s: dict, workspace: str):
        """Fallback: launch AI CLI independently when no multiplexer is available."""
        import json

        from ..adapters import get_adapter
        from ..orchestrator.nodes import get_stage_config, _render_prompt

        story_key = s["story_key"]
        stage = s["current_stage"]
        profile = s.get("profile", "minimal")

        try:
            cfg = get_stage_config(profile, stage)
            profile_data = load_profile(profile)
            adapter_name = cfg.get("cli", profile_data.get("cli", "claude"))
            model = cfg.get("model", "sonnet")
            adapter = get_adapter(adapter_name)

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
            prompt = _render_prompt(stage, state)

            tmp = Path(tempfile.gettempdir()) / f"story-prompt-{story_key}-{stage}.md"
            tmp.write_text(prompt, encoding="utf-8")

            launch = adapter.launch_cmd(model)
            db.log_stage(story_key, stage, "execute", "Direct launch (no multiplexer)")

            ttyd.launch_cli(story_key, workspace, launch, str(tmp))
        except Exception as e:
            panel = self.query_one("#detail-panel")
            panel.update(f"[red]Failed to launch CLI: {e}[/]")
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
                panel = self.query_one("#plan-panel")
                panel.update(
                    f"[bold]{key}[/]  [dim]design  │[/]  "
                    f"[bold cyan]|[/] [dim]正在规划中...[/]"
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
            prompt = _render_prompt(stage, state)
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
