"""Interactive TUI board — Textual App for story management."""

from __future__ import annotations

import subprocess
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Static
from textual.reactive import reactive

from ..db import models as db
from ..orchestrator.service import (
    get_story_cli_model,
    pause_story,
    fail_story,
    skip_stage,
)
from ..orchestrator.nodes import load_profile


class StoryCard(Static):
    """A single story card in the board."""

    def __init__(self, story: dict, selected: bool = False):
        self.story = story
        self._selected = selected
        super().__init__()

    def set_selected(self, selected: bool):
        self._selected = selected
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

        badge = {
            "active": "[bold green]> active[/]",
            "paused": "[bold yellow]|| paused[/]",
            "blocked": "[bold red]X blocked[/]",
            "completed": "[dim green]OK done[/]",
        }.get(status, status)

        try:
            profile = load_profile(profile_name)
            stages = list(profile.get("stages", {}).keys())
            bar = _render_stage_bar(stages, stage)
        except FileNotFoundError:
            bar = stage

        cli_info = get_story_cli_model(key)
        cli_line = f"  CLI: {cli_info['cli']} · Model: {cli_info['model']}"

        cursor = "[bold cyan]▸[/] " if self._selected else "  "

        lines = [
            f"{cursor}[bold cyan]{key}[/]  {title}",
            f"  {bar}  {badge}  retries: {retries}",
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


class StoryBoardApp(App):
    """Interactive story board TUI."""

    CSS = """
    Screen {
        layout: vertical;
    }
    #header-bar {
        height: 3;
        padding: 0 1;
        border-bottom: solid green;
    }
    #story-list {
        height: 1fr;
        padding: 0 1;
        overflow-y: auto;
    }
    #detail-panel {
        height: 0;
        padding: 0 1;
        border-top: solid green;
        display: none;
    }
    #detail-panel.visible {
        height: auto;
        max-height: 12;
        display: block;
    }
    #footer-bar {
        height: 2;
        padding: 0 1;
        border-top: solid green;
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
        Binding("question_mark", "help", "Help", key_display="?"),
        Binding("q", "quit", "Quit"),
    ]

    selected_index: reactive[int] = reactive(0)
    stories: reactive[list[dict]] = reactive([])

    def compose(self) -> ComposeResult:
        yield Static(id="header-bar")
        yield VerticalScroll(id="story-list")
        yield Static(id="detail-panel")
        yield Static(id="footer-bar")

    def on_mount(self):
        self._watchdog_interval = 3
        self._show_detail = False
        self.refresh_stories()
        self.set_interval(5, self.refresh_stories)
        self.set_interval(3, self.watchdog_check)

    def refresh_stories(self):
        self.stories = db.list_active_stories()
        self._render()

    def _render(self):
        from ..orchestrator.router import llm_is_available
        from .setup import get_config

        config = get_config()
        provider = config.get("provider", "N/A")
        router_status = f"enabled ({provider})" if llm_is_available() else "disabled"
        active = len([s for s in self.stories if s["status"] == "active"])
        header = self.query_one("#header-bar")
        header.update(
            f"[bold]Story Lifecycle[/]  ·  LLM Router: {router_status}  ·  Stories: {active} active"
        )

        story_list = self.query_one("#story-list")
        story_list.remove_children()
        if not self.stories:
            story_list.mount(
                Static("[dim]No active stories. Press [n] to create one.[/]")
            )
        else:
            for i, s in enumerate(self.stories):
                card = StoryCard(s, selected=(i == self.selected_index))
                story_list.mount(card)

        footer = self.query_one("#footer-bar")
        if self.stories and 0 <= self.selected_index < len(self.stories):
            key = self.stories[self.selected_index]["story_key"]
            footer.update(
                f"[n] new  [e] enter  [s] skip  [f] fail  [r] resume  [R] refresh  [?] help\n"
                f"> {key} selected. Press Enter for actions."
            )
        else:
            footer.update(
                "[n] new  [e] enter  [s] skip  [f] fail  [r] resume  [R] refresh  [?] help"
            )

    def action_cursor_up(self):
        if self.selected_index > 0:
            self.selected_index -= 1
            self._render()

    def action_cursor_down(self):
        if self.stories and self.selected_index < len(self.stories) - 1:
            self.selected_index += 1
            self._render()

    def action_open_action_menu(self):
        pass  # Direct key bindings serve as action shortcuts

    def action_enter_terminal(self):
        if not self.stories:
            return
        s = self.stories[self.selected_index]
        session = f"s-{s['story_key']}"
        self.suspend()
        subprocess.run(["tmux", "attach", "-t", session])

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
        pass  # Placeholder for new story dialog

    def action_skip_stage(self):
        if not self.stories:
            return
        s = self.stories[self.selected_index]
        skip_stage(s["story_key"], s["current_stage"])
        self.refresh_stories()

    def action_fail_story(self):
        if not self.stories:
            return
        s = self.stories[self.selected_index]
        fail_story(s["story_key"])
        self.refresh_stories()

    def action_resume_story(self):
        if not self.stories:
            return
        s = self.stories[self.selected_index]
        db.update_story(s["story_key"], status="active")
        self.refresh_stories()

    def action_refresh(self):
        self.refresh_stories()

    async def watchdog_check(self):
        from ..orchestrator.graph import resume_story

        active = [s for s in self.stories if s["status"] == "active"]
        for s in active:
            key = s["story_key"]
            stage = s["current_stage"]
            ws = s["workspace"]
            done_file = Path(ws) / ".story-done" / key / f"{stage}.json"

            if done_file.exists():
                try:
                    resume_story(key)
                except Exception:
                    pass

        new_interval = 3 if active else 30
        if new_interval != self._watchdog_interval:
            self._watchdog_interval = new_interval

    def action_quit(self):
        active = [s for s in self.stories if s["status"] == "active"]
        for s in active:
            pause_story(s["story_key"])
        self.exit()

    def action_help(self):
        self._show_detail = True
        panel = self.query_one("#detail-panel")
        panel.update(
            "[bold]Key Bindings[/]\n"
            "  ↑/k     Move up\n"
            "  ↓/j     Move down\n"
            "  Enter   Action menu\n"
            "  n       New story\n"
            "  e       Enter terminal\n"
            "  d       Toggle detail\n"
            "  s       Skip stage\n"
            "  f       Mark failed\n"
            "  r       Resume\n"
            "  R/F5    Refresh\n"
            "  ?       Help\n"
            "  q       Quit"
        )
        panel.set_class(True, "visible")


def run_tui():
    """Entry point for the TUI board."""
    from ..db.models import init_db

    init_db()
    app = StoryBoardApp()
    app.run()
