"""Doctor — check system environment and available CLI tools."""

import shutil
import subprocess

from rich.console import Console
from rich.table import Table
from rich.panel import Panel

console = Console()

CLI_TOOLS = {
    "claude": {
        "name": "Claude Code",
        "check": lambda: _which("claude"),
        "install_hint": "npm install -g @anthropic-ai/claude-code",
        "install_cmds": {
            "npm": ["npm", "install", "-g", "@anthropic-ai/claude-code"],
        },
        "homepage": "https://claude.ai/code",
    },
    "codex": {
        "name": "Codex CLI",
        "check": lambda: _which("codex"),
        "install_hint": "npm install -g @openai/codex",
        "install_cmds": {
            "npm": ["npm", "install", "-g", "@openai/codex"],
        },
        "homepage": "https://developers.openai.com/codex/cli",
    },
    "qoder": {
        "name": "Qoder CLI",
        "check": lambda: _which("qodercli"),
        "install_hint": "curl -fsSL https://qoder.com/install | bash",
        "install_cmds": {
            "_shell": "curl -fsSL https://qoder.com/install | bash",
        },
        "homepage": "https://qoder.com",
    },
    "aider": {
        "name": "Aider",
        "check": lambda: _which("aider"),
        "install_hint": "pip install aider-chat",
        "install_cmds": {
            "pip": ["pip", "install", "aider-chat"],
            "pip3": ["pip3", "install", "aider-chat"],
        },
        "homepage": "https://aider.chat",
    },
    "gemini": {
        "name": "Gemini CLI",
        "check": lambda: _which("gemini"),
        "install_hint": "npm install -g @google/gemini-cli",
        "install_cmds": {
            "npm": ["npm", "install", "-g", "@google/gemini-cli"],
            "brew": ["brew", "install", "gemini-cli"],
        },
        "homepage": "https://github.com/google-gemini/gemini-cli",
    },
}

INFRA_TOOLS = {
    "zellij": {
        "name": "Zellij",
        "check": lambda: _which("zellij"),
        "install_hint": "cargo install zellij / brew install zellij / winget install zellij",
        "install_cmds": {
            "brew": ["brew", "install", "zellij"],
            "cargo": ["cargo", "install", "zellij"],
            "winget": ["winget", "install", "--id", "Zellij.Zellij", "-e"],
        },
        "required": False,
    },
    "tmux": {
        "name": "tmux (fallback)",
        "check": lambda: _which("tmux"),
        "install_hint": "apt install tmux / brew install tmux",
        "install_cmds": {
            "apt-get": ["sudo", "apt-get", "install", "-y", "tmux"],
            "brew": ["brew", "install", "tmux"],
        },
        "required": False,
    },
    "ttyd": {
        "name": "ttyd",
        "check": lambda: _which("ttyd"),
        "install_hint": "apt install ttyd / brew install ttyd",
        "install_cmds": {
            "apt-get": ["sudo", "apt-get", "install", "-y", "ttyd"],
            "brew": ["brew", "install", "ttyd"],
        },
        "required": False,
    },
    "git": {
        "name": "Git",
        "check": lambda: _which("git"),
        "install_hint": "apt install git / brew install git",
        "install_cmds": {
            "apt-get": ["sudo", "apt-get", "install", "-y", "git"],
            "brew": ["brew", "install", "git"],
        },
        "required": False,
    },
    "python": {
        "name": "Python 3.10+",
        "check": lambda: _check_python(),
        "install_hint": "",
        "install_cmds": {},
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
        r = subprocess.run(
            [cmd, "--version"], capture_output=True, text=True, timeout=10
        )
        return r.stdout.strip().split("\n")[0][:50]
    except Exception:
        return ""


def detect_package_managers() -> dict[str, str]:
    """Detect available package managers on this system."""
    managers = {}
    for name in ("brew", "apt-get", "npm", "pip", "pip3", "winget", "cargo"):
        path = shutil.which(name)
        if path:
            managers[name] = path
    return managers


def has_missing_tools() -> bool:
    """Check if any important tools are missing."""
    for tool in CLI_TOOLS.values():
        if not tool["check"]():
            return True
    return False


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
        status = (
            "[green]OK[/]"
            if ok
            else ("[red]MISS[/]" if tool.get("required") else "[dim]N/A[/]")
        )
        version = _get_version(key) if ok else ""
        infra_table.add_row(status, tool["name"], version, tool.get("install_hint", ""))

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
        cli_table.add_row(status, tool["name"], version, tool.get("install_hint", ""))
        if ok:
            available.append(key)

    console.print(cli_table)

    # Summary
    console.print()
    if not available:
        console.print(
            Panel(
                "[yellow]No AI CLI tools detected.[/]\n\n"
                "Install at least one:\n"
                "  Claude Code: npm install -g @anthropic-ai/claude-code\n"
                "  Codex CLI:   npm install -g @openai/codex\n"
                "  Qoder CLI:   curl -fsSL https://qoder.com/install | bash\n"
                "  Aider:       pip install aider-chat\n"
                "  Gemini CLI:  npm install -g @google/gemini-cli\n\n"
                "Or run [bold]story --fix[/] to auto-install.",
                title="Warning",
                border_style="yellow",
            )
        )
    else:
        console.print(
            Panel(
                f"[green]{len(available)} CLI tools available:[/] {', '.join(available)}\n\n"
                f"Default CLI: [bold]{available[0]}[/]\n"
                f"Change with: [bold]story setup[/] or edit [bold]~/.story-lifecycle/config.yaml[/]",
                border_style="green",
            )
        )

    # Multiplexer check
    has_mplex = INFRA_TOOLS["zellij"]["check"]() or INFRA_TOOLS["tmux"]["check"]()
    if not has_mplex:
        console.print()
        console.print(
            Panel(
                "[yellow]No terminal multiplexer found.[/]\n\n"
                "Install Zellij (recommended) or tmux:\n"
                "  Zellij:  cargo install zellij / brew install zellij\n"
                "  tmux:    apt install tmux / brew install tmux\n\n"
                "Without a multiplexer, AI terminal sessions won't launch.\n"
                "Run [bold]story --fix[/] to auto-install.",
                title="Warning",
                border_style="yellow",
            )
        )


def run_doctor_fix(interactive: bool = True):
    """Check and auto-install missing tools."""
    console.print()
    console.print("[bold]Story Lifecycle Doctor --fix[/]\n")

    managers = detect_package_managers()
    if not managers:
        console.print("[red]No package managers detected. Cannot auto-install.[/]")
        console.print("Install npm (Node.js), pip, brew, or apt to proceed.")
        return

    console.print(f"[dim]Package managers: {', '.join(managers.keys())}[/]\n")

    installed_ok = []
    installed_fail = []
    skipped = []

    all_tools = list(CLI_TOOLS.items()) + list(INFRA_TOOLS.items())

    for key, tool in all_tools:
        if tool["check"]():
            continue

        install_cmds = tool.get("install_cmds", {})
        if not install_cmds:
            skipped.append((tool["name"], "no auto-install command"))
            continue

        # Find matching package manager
        cmd = None
        pm_name = None
        for pm in install_cmds:
            if pm == "_shell":
                if shutil.which("curl") or shutil.which("bash"):
                    cmd = install_cmds[pm]
                    pm_name = "shell"
                    break
            elif pm in managers:
                cmd = install_cmds[pm]
                pm_name = pm
                break

        if cmd is None:
            skipped.append(
                (tool["name"], f"no supported PM ({', '.join(install_cmds.keys())})")
            )
            continue

        # Ask confirmation
        if interactive:
            console.print(f"  [yellow]MISSING[/] {tool['name']}")
            answer = console.input(f"    Install via {pm_name}? [Y/n] ").strip().lower()
            if answer not in ("", "y", "yes"):
                skipped.append((tool["name"], "user declined"))
                continue
        else:
            console.print(f"  [yellow]INSTALLING[/] {tool['name']} via {pm_name}...")

        # Run install
        console.print(
            f"    [dim]Running: {cmd if isinstance(cmd, str) else ' '.join(cmd)}[/]"
        )
        try:
            if isinstance(cmd, str):
                result = subprocess.run(
                    cmd, shell=True, capture_output=True, text=True, timeout=120
                )
            else:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=120
                )
            if result.returncode == 0 and tool["check"]():
                installed_ok.append(tool["name"])
                console.print(f"    [green]OK[/] {tool['name']} installed successfully")
            else:
                err = result.stderr[:200] if result.stderr else "verification failed"
                installed_fail.append((tool["name"], err))
                console.print(f"    [red]FAIL[/] {tool['name']}: {err}")
        except subprocess.TimeoutExpired:
            installed_fail.append((tool["name"], "timed out"))
            console.print(f"    [red]FAIL[/] {tool['name']}: install timed out")
        except Exception as e:
            installed_fail.append((tool["name"], str(e)[:200]))
            console.print(f"    [red]FAIL[/] {tool['name']}: {e}")

    # Summary
    console.print()
    if installed_ok:
        console.print(
            f"[green]Installed ({len(installed_ok)}):[/] {', '.join(installed_ok)}"
        )
    if installed_fail:
        names = ", ".join(n for n, _ in installed_fail)
        console.print(f"[red]Failed ({len(installed_fail)}):[/] {names}")
    if skipped:
        names = ", ".join(n for n, _ in skipped)
        console.print(f"[dim]Skipped ({len(skipped)}):[/] {names}")
    if not installed_ok and not installed_fail and not skipped:
        console.print("[green]All tools already installed.[/]")
