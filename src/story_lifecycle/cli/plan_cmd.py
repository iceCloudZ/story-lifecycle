"""CLI command group for `story plan` — AI-assisted project planning."""

from __future__ import annotations


import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()


@click.group()
def plan():
    """AI-assisted project planning."""
    pass


@plan.command()
@click.option("--cwd", default=".", help="Working directory")
def init(cwd: str):
    """Probe project state and guide next steps."""
    from ..planner.probe import probe_project
    from ..planner.state import get_resume_info

    # Check for incomplete planning flow
    resume = get_resume_info(cwd=cwd)
    if resume and resume["completed_steps"]:
        console.print(
            Panel.fit(
                f"[yellow]检测到未完成的规划流程[/]\n\n"
                f"已完成: {', '.join(resume['completed_steps'])}\n"
                f"当前步骤: {resume['current_step']}\n\n"
                f"要继续吗？",
                title="断点续传",
                border_style="yellow",
            )
        )
        if not click.confirm("继续规划？", default=True):
            return

    result = probe_project(cwd=cwd)
    phase = result["phase"]
    signals = result["signals"]
    suggested = result["suggested_step"]

    # Display detected signals
    table = Table(title="项目状态探测")
    table.add_column("信号", style="cyan")
    table.add_column("状态", style="green")
    signal_labels = {
        "has_git": "Git 仓库",
        "has_story_dir": ".story/ 目录",
        "has_code": "代码文件",
        "has_planning_dir": ".story/planning/",
        "has_requirements": "requirements.md",
        "has_roadmap": "roadmap.md",
        "has_issues_json": "issues.json",
    }
    for key, label in signal_labels.items():
        status = "✓" if signals.get(key) else "✗"
        table.add_row(label, status)
    console.print(table)

    # Suggest next step
    step_map = {
        "step_0a": ("从 idea 生成需求文档", "story plan idea"),
        "step_1": ("生成开发路线图", "story plan roadmap"),
        "step_2": ("拆解里程碑为 Issue", "story plan decompose"),
        "execute": ("Issue 已就绪，进入 Phase 1 执行", "story serve"),
    }
    desc, cmd = step_map.get(suggested, ("未知步骤", ""))
    console.print()
    console.print(
        Panel.fit(
            f"当前阶段: [bold]{phase}[/]\n"
            f"建议下一步: [bold]{desc}[/]\n\n"
            f"运行: [bold]{cmd}[/]",
            title="规划建议",
            border_style="green",
        )
    )

    # If empty project, enter idea dialog
    if phase == "empty":
        console.print("\n[bold]检测到空项目。开始 idea → requirements 流程：[/]\n")
        idea = click.prompt("请描述你的 idea")
        _run_idea_expander(idea, cwd=cwd)


@plan.command()
@click.option("--idea", "-i", help="Your project idea (if not interactive)")
@click.option("--cwd", default=".", help="Working directory")
def idea(idea: str | None, cwd: str):
    """Expand an idea into requirements document."""
    if not idea:
        idea = click.prompt("请描述你的 idea")
    _run_idea_expander(idea, cwd=cwd)


def _run_idea_expander(idea_text: str, *, cwd: str):
    from ..planner.idea_expander import expand_idea_to_requirements

    console.print("[dim]正在生成需求文档...[/]")
    try:
        content = expand_idea_to_requirements(idea_text, cwd=cwd)
        console.print(
            Panel.fit(
                content[:500] + ("..." if len(content) > 500 else ""),
                title="requirements.md 草稿",
                border_style="green",
            )
        )
        console.print("\n需求文档已保存到 [bold].story/planning/requirements.md[/]")
        console.print("下一步: [bold]story plan roadmap[/]")
    except Exception as e:
        console.print(f"[red]生成需求文档失败: {e}[/]")
        console.print("[dim]请确认已配置 LLM API key (运行 story setup)[/]")


@plan.command()
@click.option("--from", "from_file", help="Input requirements file path")
@click.option("--cwd", default=".", help="Working directory")
def roadmap(from_file: str | None, cwd: str):
    """Generate a phased roadmap from requirements."""
    from ..planner.roadmap import generate_roadmap

    console.print("[dim]正在生成路线图...[/]")
    try:
        content = generate_roadmap(requirements_path=from_file, cwd=cwd)
        console.print(
            Panel.fit(
                content[:800] + ("..." if len(content) > 800 else ""),
                title="roadmap.md 草稿",
                border_style="green",
            )
        )
        console.print("\n路线图已保存到 [bold].story/planning/roadmap.md[/]")
        console.print("下一步: [bold]story plan decompose[/]")
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/]")
        console.print("[dim]请先运行 story plan idea 生成需求文档[/]")
    except Exception as e:
        console.print(f"[red]生成路线图失败: {e}[/]")


@plan.command()
@click.option("--phase", type=int, help="Phase number to decompose")
@click.option("--cwd", default=".", help="Working directory")
def decompose(phase: int | None, cwd: str):
    """Decompose a roadmap phase into Issue drafts."""
    from ..planner.decomposer import decompose_phase

    console.print("[dim]正在拆解 Issue 草稿...[/]")
    try:
        issues = decompose_phase(phase_number=phase, cwd=cwd)
        console.print(f"\n[green]生成了 {len(issues)} 个 Issue 草稿[/]\n")

        for i, issue in enumerate(issues, 1):
            console.print(f"  {i}. [bold]{issue.get('title', 'Untitled')}[/]")
            labels = issue.get("labels", [])
            if labels:
                console.print(f"     labels: {', '.join(labels)}")

        console.print("\nIssue 草稿已保存到 [bold].story/planning/issues.json[/]")
        console.print(
            "下一步: [bold]story plan publish[/] 或 [bold]story plan publish --dry-run[/] 预览"
        )
    except (FileNotFoundError, ValueError) as e:
        console.print(f"[red]{e}[/]")
    except Exception as e:
        console.print(f"[red]拆解失败: {e}[/]")


@plan.command()
@click.option("--repo", required=True, help="GitHub repo (owner/repo)")
@click.option("--dry-run", is_flag=True, help="Preview without creating")
@click.option("--cwd", default=".", help="Working directory")
def publish(repo: str, dry_run: bool, cwd: str):
    """Batch create GitHub Issues from drafts."""
    from ..planner.publisher import publish_issues

    if dry_run:
        console.print("[yellow]DRY RUN — 不会实际创建 Issue[/]\n")

    try:
        results = publish_issues(repo, cwd=cwd, dry_run=dry_run)
        created = [r for r in results if r.get("number")]
        failed = [r for r in results if r.get("error")]

        if created:
            console.print(f"\n[green]创建了 {len(created)} 个 Issue:[/]")
            for r in created:
                console.print(f"  #{r['number']}: {r['title']}")
                if r.get("url"):
                    console.print(f"    {r['url']}")

        if failed:
            console.print(f"\n[red]{len(failed)} 个 Issue 创建失败:[/]")
            for r in failed:
                console.print(f"  {r['title']}: {r['error']}")

        if not dry_run and created:
            console.print(
                "\nIssue 已添加 [bold]lifecycle:accepted[/] 标签，可被 Phase 1 自动拉取"
            )
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/]")
    except Exception as e:
        console.print(f"[red]发布失败: {e}[/]")
