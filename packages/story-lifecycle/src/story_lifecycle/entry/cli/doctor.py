"""Doctor — check system environment and available CLI tools."""

import re
import shutil
import sqlite3
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


def _check_module(name: str) -> bool:
    try:
        __import__(name)
        return True
    except ImportError:
        return False


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
    console.print("[bold]AI CLI Tools[/]  [dim](只需安装其中一个即可运行)[/]")
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
                "只需安装 [bold]其中一个[/] 即可：\n"
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
                f"Change with: [bold]story setup[/] or edit [bold]~/.story-lifecycle/config.yaml[/]\n\n"
                f"[dim]只需一个 CLI 工具即可运行，其余为可选项。[/]",
                border_style="green",
            )
        )

    # Multiplexer check
    has_mplex = INFRA_TOOLS["zellij"]["check"]()
    if not has_mplex:
        console.print()
        console.print(
            Panel(
                "[yellow]No terminal multiplexer found.[/]\n\n"
                "winget install zellij\n\n"
                "AI 终端会话需要此组件。\n"
                "Run [bold]story --fix[/] to auto-install.",
                title="Warning",
                border_style="yellow",
            )
        )

    # Linkage health check
    console.print()
    run_linkage_health()


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


# ---------------------------------------------------------------------------
# Linkage health — hard association between story_key and git branches/commits
# ---------------------------------------------------------------------------

_REPOS = [
    "hc-order",
    "hc-user",
    "hc-risk-management",
    "hc-message",
    "hc-config",
    "hc-limit",
    "hc-third-party",
    "hc-coupon",
    "hc-marketing",
    "hc-callback",
    "hc-gateway",
    "hc-job",
    "hc-audit",
    "hc-aiops",
    "hc-pytest",
    "story-board",
    "ys-frame-parent",
]


def _story_db_path() -> Path:
    return Path.home() / ".story-lifecycle" / "story.db"


def _extract_short_id(story_key: str) -> str:
    m = re.search(r"(\d+)$", story_key or "")
    return m.group(1) if m else ""


def _repo_has_grep_match(repo_path: Path, short_id: str) -> bool:
    if not (repo_path / ".git").is_dir() or not short_id:
        return False
    try:
        r = subprocess.run(
            [
                "git",
                "-C",
                str(repo_path),
                "log",
                "master",
                "--grep",
                short_id,
                "--oneline",
                "-n",
                "1",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:
        return False


def run_linkage_health():
    """Report hard-linkage health: branch names include story_key and git log finds them."""
    db_path = _story_db_path()
    if not db_path.exists():
        console.print("[dim]Linkage health: story.db not found, skipping.[/]")
        return

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT DISTINCT story_key, branch FROM story_project WHERE branch IS NOT NULL AND branch != ''"
        ).fetchall()
    except Exception as exc:
        console.print(f"[red]Linkage health: failed to read story.db: {exc}[/]")
        return

    if not rows:
        console.print("[dim]Linkage health: no story branches recorded.[/]")
        return

    base = Path("D:/hc-all")
    hard_branch = 0
    orphan_branch = 0
    hard_commit = 0
    total = len(rows)

    for row in rows:
        story_key = row["story_key"]
        branch = row["branch"]
        short_id = _extract_short_id(story_key)
        is_hard_branch = short_id and short_id in branch
        if is_hard_branch:
            hard_branch += 1

        # 在 17 个子仓里找该分支或 grep story_key
        found_repo = None
        if is_hard_branch:
            for repo_name in _REPOS:
                repo_path = base / repo_name
                if not (repo_path / ".git").is_dir():
                    continue
                if _repo_has_grep_match(repo_path, short_id):
                    found_repo = repo_name
                    break
        else:
            # 旧规则分支：直接 rev-parse 分支存在性
            for repo_name in _REPOS:
                repo_path = base / repo_name
                if not (repo_path / ".git").is_dir():
                    continue
                try:
                    r = subprocess.run(
                        ["git", "-C", str(repo_path), "rev-parse", "--verify", branch],
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=10,
                    )
                    if r.returncode == 0:
                        found_repo = repo_name
                        break
                except Exception:
                    pass

        if found_repo:
            hard_commit += 1
        else:
            orphan_branch += 1

    pct_hard_branch = round(100.0 * hard_branch / total, 1) if total else 0.0
    pct_hard_commit = round(100.0 * hard_commit / total, 1) if total else 0.0

    console.print("[bold]Linkage Health[/]")
    table = Table(show_header=True, padding=(0, 1))
    table.add_column("Metric")
    table.add_column("Count")
    table.add_column("Rate")
    table.add_row("Stories with branch", str(total), "—")
    table.add_row(
        "Hard branch (story_key in branch)",
        f"{hard_branch}/{total}",
        f"{pct_hard_branch}%",
    )
    table.add_row(
        "Merge commit reachable (git log --grep)",
        f"{hard_commit}/{total}",
        f"{pct_hard_commit}%",
    )
    table.add_row("Orphan / not found", str(orphan_branch), "—")
    console.print(table)

    if pct_hard_branch < 50:
        console.print(
            "[yellow]  Tip: hard linkage is low. New stories use branch_rule with {story_key} "
            "(e.g. feature/{author}/{story_key}_{summary}_{date}).[/]"
        )
