"""CLI command group for `story plan` — AI-assisted project planning."""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.status import Status
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

    # If empty or no requirements, generate requirements
    if phase == "empty":
        console.print("\n[bold]检测到空项目。请描述你的 idea：[/]\n")
        idea = click.prompt("你的 idea")
        _interactive_requirements(idea_text=idea, cwd=cwd)
    elif phase == "has_code_no_plan":
        console.print("\n[bold]检测到已有代码，正在用 AI 分析项目...[/]\n")
        _interactive_requirements(cwd=cwd)


@plan.command()
@click.option("--idea", "-i", help="Your project idea (if not interactive)")
@click.option("--cwd", default=".", help="Working directory")
def idea(idea: str | None, cwd: str):
    """Expand an idea into requirements document."""
    if not idea:
        idea = click.prompt("请描述你的 idea")
    _interactive_requirements(idea_text=idea, cwd=cwd)


def _interactive_requirements(*, idea_text: str | None = None, cwd: str) -> None:
    """Generate requirements with interactive review loop.

    Flow: generate → display full content → (Y)保存 / (e)编辑 / (r)重试 / (q)退出
    """
    content = _generate_requirements(idea_text=idea_text, cwd=cwd)
    if content is None:
        return

    while True:
        # Show full content
        console.print()
        console.print(
            Panel(Markdown(content), title="requirements.md 草稿", border_style="green")
        )

        console.print(
            "\n[bold](Y)[/] 确认保存  [bold](e)[/] 补充修改  [bold](r)[/] 重新生成  [bold](q)[/] 退出"
        )
        choice = click.prompt("选择", default="Y").strip().lower()

        if choice in ("y", ""):
            _save_planning_file(content, "requirements.md", cwd=cwd)
            console.print(
                "\n需求文档已保存到 [bold].story/planning/requirements.md[/]\n"
                "下一步: [bold]story plan roadmap[/]"
            )
            return
        elif choice == "e":
            feedback = click.prompt("补充或修改什么")
            content = _generate_requirements(
                idea_text=idea_text, cwd=cwd, previous_draft=content, feedback=feedback
            )
            if content is None:
                return
        elif choice == "r":
            content = _generate_requirements(idea_text=idea_text, cwd=cwd)
            if content is None:
                return
        elif choice == "q":
            console.print("[dim]已取消[/]")
            return


def _generate_requirements(
    *,
    idea_text: str | None = None,
    cwd: str,
    previous_draft: str | None = None,
    feedback: str | None = None,
) -> str | None:
    """Call LLM to generate requirements. Returns content or None on failure."""
    from ..planner.idea_expander import (
        analyze_codebase_to_requirements,
        expand_idea_to_requirements,
    )

    try:
        with Status("[bold green]AI 正在思考...", console=console, spinner="dots"):
            if idea_text is not None:
                if previous_draft:
                    content = expand_idea_to_requirements(
                        f"{idea_text}\n\n对上一版草稿的反馈：{feedback}\n\n上一版草稿：\n{previous_draft}",
                        cwd=cwd,
                    )
                else:
                    content = expand_idea_to_requirements(idea_text, cwd=cwd)
            else:
                if previous_draft:
                    content = analyze_codebase_to_requirements(
                        cwd=cwd,
                        previous_draft=previous_draft,
                        feedback=feedback,
                    )
                else:
                    content = analyze_codebase_to_requirements(cwd=cwd)
        return content
    except Exception as e:
        console.print(f"[red]生成失败: {e}[/]")
        console.print("[dim]请确认已配置 LLM API key (运行 story setup)[/]")
        return None


@plan.command()
@click.option("--from", "from_file", help="Input requirements file path")
@click.option("--cwd", default=".", help="Working directory")
def roadmap(from_file: str | None, cwd: str):
    """Generate a phased roadmap from requirements."""
    from ..planner.roadmap import generate_roadmap

    try:
        with Status(
            "[bold green]AI 正在生成路线图...", console=console, spinner="dots"
        ):
            content = generate_roadmap(requirements_path=from_file, cwd=cwd)
        _interactive_review(
            content,
            filename="roadmap.md",
            title="roadmap.md 草稿",
            next_step="story plan decompose",
            cwd=cwd,
        )
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

    try:
        with Status(
            "[bold green]AI 正在拆解 Issue 草稿...", console=console, spinner="dots"
        ):
            issues = decompose_phase(phase_number=phase, cwd=cwd)
        console.print(f"\n[green]生成了 {len(issues)} 个 Issue 草稿[/]\n")

        for i, issue in enumerate(issues, 1):
            console.print(f"  {i}. [bold]{issue.get('title', 'Untitled')}[/]")
            labels = issue.get("labels", [])
            if labels:
                console.print(f"     labels: {', '.join(labels)}")

        _interactive_review(
            _format_issues_display(issues),
            filename="issues.json",
            title="Issue 草稿",
            next_step="story plan publish --dry-run",
            cwd=cwd,
            raw_content=_serialize_issues(issues),
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


def _interactive_review(
    content: str,
    *,
    filename: str,
    title: str,
    next_step: str,
    cwd: str,
    raw_content: str | None = None,
) -> None:
    """Show generated content and ask for confirmation before saving."""
    console.print()
    console.print(Panel(Markdown(content), title=title, border_style="green"))

    console.print("\n[bold](Y)[/] 确认保存  [bold](q)[/] 退出不保存")
    choice = click.prompt("选择", default="Y").strip().lower()

    if choice in ("y", ""):
        save_data = raw_content if raw_content else content
        _save_planning_file(save_data, filename, cwd=cwd)
        console.print(
            f"\n已保存到 [bold].story/planning/{filename}[/]\n"
            f"下一步: [bold]{next_step}[/]"
        )
    else:
        console.print("[dim]已取消，未保存[/]")


def _save_planning_file(content: str, filename: str, *, cwd: str) -> Path:
    """Save content to .story/planning/filename."""
    root = Path(cwd) if cwd else Path.cwd()
    planning_dir = root / ".story" / "planning"
    planning_dir.mkdir(parents=True, exist_ok=True)
    path = planning_dir / filename
    path.write_text(content, encoding="utf-8")
    return path


def _format_issues_display(issues: list[dict]) -> str:
    """Format issues list as markdown for display."""
    lines = []
    for i, issue in enumerate(issues, 1):
        title = issue.get("title", "Untitled")
        labels = ", ".join(issue.get("labels", []))
        body_preview = (issue.get("body", "") or "")[:200]
        lines.append(f"### {i}. {title}")
        if labels:
            lines.append(f"Labels: {labels}")
        lines.append(f"{body_preview}...\n")
    return "\n".join(lines)


def _serialize_issues(issues: list[dict]) -> str:
    """Serialize issues to JSON string."""
    import json

    return json.dumps(issues, ensure_ascii=False, indent=2)
