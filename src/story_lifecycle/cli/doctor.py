"""Doctor — check system environment and available CLI tools."""

import shutil
import subprocess
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.panel import Panel

console = Console()

CLI_TOOLS = {
    "claude": {
        "name": "Claude Code",
        "check": lambda: _which("claude"),
        "install": "npm install -g @anthropic-ai/claude-code",
        "homepage": "https://claude.ai/code",
    },
    "codex": {
        "name": "Codex CLI",
        "check": lambda: _which("codex"),
        "install": "npm install -g @anthropic-ai/codex-cli",
        "homepage": "https://github.com/anthropics/codex-cli",
    },
    "qoder": {
        "name": "Qoder CLI",
        "check": lambda: _which("qodercli"),
        "install": "curl -fsSL https://qoder.com/install | bash",
        "homepage": "https://qoder.com",
    },
    "aider": {
        "name": "Aider",
        "check": lambda: _which("aider"),
        "install": "pip install aider-chat",
        "homepage": "https://aider.chat",
    },
    "gemini": {
        "name": "Gemini CLI",
        "check": lambda: _which("gemini"),
        "install": "npm install -g @anthropic-ai/gemini-cli",
        "homepage": "https://deepmind.google/technologies/gemini/",
    },
}

INFRA_TOOLS = {
    "tmux": {
        "name": "tmux",
        "check": lambda: _which("tmux"),
        "install": "apt install tmux (Linux) / brew install tmux (macOS)",
        "required": True,
    },
    "ttyd": {
        "name": "ttyd",
        "check": lambda: _which("ttyd"),
        "install": "apt install ttyd (Linux) / brew install ttyd (macOS)",
        "required": True,
    },
    "git": {
        "name": "Git",
        "check": lambda: _which("git"),
        "install": "apt install git / brew install git",
        "required": False,
    },
    "python": {
        "name": "Python 3.10+",
        "check": lambda: _check_python(),
        "install": "",
        "required": True,
    },
}


def _which(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _check_python() -> bool:
    import sys
    return sys.version_info >= (3, 10)


def _get_version(cmd: str) -> str:
    try:
        r = subprocess.run([cmd, "--version"], capture_output=True, text=True, timeout=10)
        return r.stdout.strip().split("\n")[0][:50]
    except Exception:
        return ""


def run_doctor():
    """Check system environment and report available tools."""
    console.print()
    console.print("[bold]Story Lifecycle Doctor[/]\n")

    # Infrastructure
    console.print("[bold]Infrastructure[/]")
    infra_table = Table(show_header=False, padding=(0, 1))
    infra_table.add_column("Status", width=6)
    infra_table.add_column("Tool", width=10)
    infra_table.add_column("Details")
    infra_table.add_column("Install", style="dim")

    for key, tool in INFRA_TOOLS.items():
        ok = tool["check"]()
        status = "[green]OK[/]" if ok else ("[red]MISS[/]" if tool.get("required") else "[dim]N/A[/]")
        version = _get_version(key) if ok else ""
        infra_table.add_row(status, tool["name"], version, tool.get("install", ""))

    console.print(infra_table)

    # AI CLIs
    console.print()
    console.print("[bold]AI CLI Tools[/]")
    cli_table = Table(show_header=False, padding=(0, 1))
    cli_table.add_column("Status", width=6)
    cli_table.add_column("Tool", width=15)
    cli_table.add_column("Version")
    cli_table.add_column("How to install", style="dim")

    available = []
    for key, tool in CLI_TOOLS.items():
        ok = tool["check"]()
        status = "[green]OK[/]" if ok else "[dim]--[/]"
        version = _get_version(key) if ok else ""
        cli_table.add_row(status, tool["name"], version, tool.get("install", ""))
        if ok:
            available.append(key)

    console.print(cli_table)

    # Summary
    console.print()
    if not available:
        console.print(Panel(
            "[yellow]No AI CLI tools detected.[/]\n\n"
            "Install at least one:\n"
            "  Claude Code: npm install -g @anthropic-ai/claude-code\n"
            "  Codex CLI:   npm install -g @anthropic-ai/codex-cli\n"
            "  Qoder CLI:   curl -fsSL https://qoder.com/install | bash\n"
            "  Aider:       pip install aider-chat",
            title="Warning",
            border_style="yellow",
        ))
    else:
        console.print(Panel(
            f"[green]{len(available)} CLI tools available:[/] {', '.join(available)}\n\n"
            f"Default CLI: [bold]{available[0]}[/]\n"
            f"Change with: [bold]story setup[/] or edit [bold]~/.story-lifecycle/config.yaml[/]",
            border_style="green",
        ))

    # tmux/ttyd check
    if not INFRA_TOOLS["tmux"]["check"]() or not INFRA_TOOLS["ttyd"]["check"]():
        console.print()
        console.print(Panel(
            "[yellow]tmux and/or ttyd are missing.[/]\n\n"
            "These are required for AI execution (terminal sharing).\n"
            "Without them, CLI and DB operations will work, but the AI won't launch.\n\n"
            "Linux:   sudo apt install tmux ttyd\n"
            "macOS:   brew install tmux ttyd\n"
            "Windows: Use WSL2 (see README for details)",
            title="Warning",
            border_style="yellow",
        ))
