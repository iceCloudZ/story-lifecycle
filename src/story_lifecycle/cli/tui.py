"""Interactive TUI board — Textual App for story management."""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

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
    pause_story,
    fail_story,
    skip_stage,
    create_and_start_story,
    delete_story,
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

    def __init__(self, story: dict, selected: bool = False, collapsed: bool = False, sub_count: int = 0):
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
            colors = {"bug-fix": "red", "integration": "yellow", "refinement": "blue", "redo": "orange"}
            color = colors.get(sub_type, "grey")
            type_badge = f" [{color}][{sub_type}][/{color}]"

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
            yield Input(placeholder="e.g. hotfix (leave empty to use selection)", id="input-custom-type")
            yield Static("Start Stage (empty = auto):")
            yield Input(placeholder="e.g. implement (auto-derived from type)", id="input-stage")
            yield Static("Description:")
            first_template = list(self._type_configs.values())[0].get("description_template", "")
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
                default_stage = self._type_configs[selected_key].get("default_start_stage", "")

            custom_stage = self.query_one("#input-stage", Input).value.strip()
            self.dismiss({
                "sub_type": sub_type,
                "start_stage": custom_stage or default_stage or None,
                "description": desc,
            })
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
        self._render()

    def _render(self):
        lines = []
        for i, item in enumerate(self._items):
            check = "✓" if i in self._selected else " "
            cursor = ">" if i == self._cursor else " "
            type_tag = "[需求]" if item.item_type == "requirement" else "[Bug]"
            lines.append(f"  {cursor} [{check}] {type_tag} {item.title}  ({item.source})")
        self.query_one("#inbox-list", Static).update("\n".join(lines))

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
            self._render()

    def key_down(self):
        if self._cursor < len(self._items) - 1:
            self._cursor += 1
            self._render()

    def key_space(self):
        if self._cursor in self._selected:
            self._selected.discard(self._cursor)
        else:
            self._selected.add(self._cursor)
        self._render()

    def key_enter(self):
        if self._cursor not in self._selected:
            self._selected.add(self._cursor)
        self._render()
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
        self._render()

    def _render(self):
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
            self._render()

    def key_down(self):
        if self._cursor < len(self._stories) - 1:
            self._cursor += 1
            self._render()

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
        Binding("c", "toggle_collapse", "Fold"),
        Binding("shift+d", "run_doctor", "Doctor", key_display="D"),
        Binding("shift+s", "run_setup", "Setup", key_display="S"),
        Binding("i", "show_inbox", "Inbox"),
        Binding("question_mark", "help", "Help", key_display="?"),
        Binding("q", "quit", "Quit"),
    ]

    selected_index: reactive[int] = reactive(0)
    stories: reactive[list[dict]] = reactive([])

    def __init__(self):
        super().__init__()
        self._source_enabled = False
        self._pending_items: list = []

    def compose(self) -> ComposeResult:
        yield Static(id="header-bar")
        yield Static(id="plan-panel")
        yield VerticalScroll(id="story-list")
        yield Static(id="detail-panel")
        yield Static(id="footer-bar")
        yield Footer()

    # 4x4 braille grid spinner — 3-dot trail rotating clockwise
    _SPINNER_FRAMES: list[str] = []
    for _off in range(12):
        _bits = [0, 0]
        for _i in range(3):
            _ci, _b = [
                (0, 0x01), (0, 0x08), (1, 0x01), (1, 0x08),  # top row
                (1, 0x10), (1, 0x20),                          # right col
                (1, 0x80), (1, 0x40),                          # bottom-right
                (0, 0x80), (0, 0x40),                          # bottom-left
                (0, 0x04), (0, 0x02),                          # left col
            ][(_off + _i) % 12]
            _bits[_ci] |= _b
        _SPINNER_FRAMES.append(chr(0x2800 + _bits[0]) + chr(0x2800 + _bits[1]))

    def on_mount(self):
        set_tui_app(self)
        self._watchdog_interval = 3
        self._show_detail = False
        self._collapsed_parents: set[str] = set()
        self._plan_story_key = ""
        self._spinner_idx = -1  # -1 = stopped
        self._plan_start_time = 0.0
        self.refresh_stories()
        self.set_interval(5, self.refresh_stories)
        self.set_interval(3, self.watchdog_check)
        self.set_interval(0.08, self.tick_spinner)

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
                display_idx = 0
                for i, s in enumerate(self.stories):
                    pk = s.get("parent_key")
                    if pk and pk in self._collapsed_parents:
                        continue
                    is_parent = not bool(s.get("parent_key"))
                    sub_count = len(db.get_sub_stories(s["story_key"])) if is_parent else 0
                    card = StoryCard(
                        s,
                        selected=(display_idx == self.selected_index),
                        collapsed=(is_parent and s["story_key"] in self._collapsed_parents),
                        sub_count=sub_count,
                    )
                    story_list.mount(card)
                    display_idx += 1
        else:
            for i, card in enumerate(self.query(StoryCard)):
                card.set_selected(i == self.selected_index)

        footer = self.query_one("#footer-bar")
        footer.update(
            " [dim][n] new  [N] sub  [i] inbox  [e] enter  [s] skip  [a] abort  [f] fail  [x] delete  [r] resume  [?] help[/]"
        )

    def action_cursor_up(self):
        if self.selected_index > 0:
            self.selected_index -= 1
            self._render(full=False)

    def action_cursor_down(self):
        visible = self._visible_stories()
        if visible and self.selected_index < len(visible) - 1:
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
        if self._spinner_idx < 0:
            return
        try:
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
        skip_stage(s["story_key"], s["current_stage"])
        self.refresh_stories()

    def action_fail_story(self):
        if not self.stories:
            return
        s = self.stories[self.selected_index]
        fail_story(s["story_key"])
        self.refresh_stories()

    def action_delete_story(self):
        if not self.stories:
            return
        s = self.stories[self.selected_index]
        key = s["story_key"]

        def on_confirm(confirmed):
            if confirmed:
                delete_story(key)
                self.refresh_stories()

        self.push_screen(
            ConfirmDialog(f"Delete story {key}?"),
            on_confirm,
        )

    def action_resume_story(self):
        if not self.stories:
            return
        s = self.stories[self.selected_index]
        db.update_story(s["story_key"], status="active")
        self.refresh_stories()

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
        from ..orchestrator.service import abort_story
        try:
            abort_story(s["story_key"])
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

    async def watchdog_check(self):
        from ..orchestrator.graph import resume_story, is_story_running
        from ..db import models as db

        active = [s for s in self.stories if s["status"] == "active"]
        for s in active:
            key = s["story_key"]
            stage = s["current_stage"]
            ws = s["workspace"]
            done_file = Path(ws) / ".story-done" / key / f"{stage}.json"

            if done_file.exists() and not is_story_running(key):
                try:
                    resume_story(key)
                except Exception:
                    pass

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
        active = [s for s in self.stories if s["status"] == "active"]
        for s in active:
            try:
                pause_story(s["story_key"])
            except Exception:
                pass
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

        try:
            items = source.fetch_pending()
            items = [i for i in items if not db.find_by_source_id(i.source, i.id)]
        except Exception as e:
            self.notify(f"获取待办失败: {e}", severity="error")
            return

        if not items:
            self.notify("没有新的待办")
            return

        def _on_inbox_result(result):
            if not result:
                return
            from ..orchestrator.service import create_story_from_source
            for entry in result:
                try:
                    if isinstance(entry, tuple) and len(entry) == 2:
                        mode, item = entry
                    else:
                        mode, item = "normal", entry
                    use_ai_prd = (mode == "ai_prd")
                    r = create_story_from_source(item, auto_start=True, generate_ai_prd=use_ai_prd)
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
                                r2 = create_story_from_source(item, auto_start=True, generate_ai_prd=use_ai_prd, force_standalone=True)
                                if r2.status == "created":
                                    self.notify(f"已创建独立故事: {r2.story_key}")
                            else:
                                sub_key = create_sub_story(parent_key=parent_key, sub_type="bug-fix", description=item.description)
                                if sub_key:
                                    self.notify(f"已创建子故事: {sub_key}")

                        self.push_screen(ParentSelectDialog(item.title, active), _on_parent_selected)
                    else:
                        self.notify(f"创建失败: {r.error}", severity="error")
                except Exception as e:
                    self.notify(f"创建失败: {e}", severity="error")
            self.refresh_stories()

        screen = InboxScreen(items)
        self.push_screen(screen, _on_inbox_result)

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
            "  ?       Help\n"
            "  q       Quit"
        )
        panel.set_class(True, "visible")


def run_tui():
    """Entry point for the TUI board."""
    app = StoryBoardApp()
    app.run()
