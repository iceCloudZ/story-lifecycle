"""story project — 项目级知识包管理命令。"""

import shutil

import click
from pathlib import Path
from rich.console import Console

console = Console()

# 内置 adapter → 可执行文件名映射
_ADAPTER_EXECUTABLES = {
    "claude": "claude",
    "codex": "codex",
}


def _detect_available_adapters() -> list[str]:
    """检测环境中可用的 AI CLI，返回 adapter 名称列表。"""
    available = []
    for name, exe in _ADAPTER_EXECUTABLES.items():
        if shutil.which(exe) or shutil.which(f"{exe}.cmd"):
            available.append(name)
    # 也检查 adapters.yaml 中配置的 shell adapter
    try:
        from ..adapters import _load_adapter_configs

        for name in _load_adapter_configs():
            if name not in available:
                available.append(name)
    except Exception:
        pass
    return available


@click.group()
def project():
    """项目知识包管理。"""
    pass


@project.command("init-knowledge")
@click.option("-w", "--workspace", default=None, help="工作区目录（默认当前目录）")
@click.option("--dry-run", is_flag=True, help="只探测和展示，不写入知识文件")
@click.option("--yes", "-y", is_flag=True, help="非交互模式，自动接受推荐范围")
@click.option(
    "--include", "includes", multiple=True, help="显式包含的服务/路径（可多次使用）"
)
@click.option(
    "--exclude", "excludes", multiple=True, help="显式排除的服务/路径（可多次使用）"
)
@click.option(
    "--codegraph",
    type=click.Choice(["optional", "off"]),
    default="optional",
    help="CodeGraph provider 模式（默认 optional）",
)
@click.option(
    "--legacy",
    is_flag=True,
    help="使用旧的 AI CLI 扫描模式（启动 Claude/codex 执行扫描）",
)
@click.option(
    "--scan-profile",
    default="java-spring-microservice",
    help="[legacy 模式] 扫描 profile",
)
@click.option("--adapter", default=None, help="[legacy 模式] AI CLI adapter")
@click.option(
    "--timeout", default=1800, type=int, help="[legacy 模式] headless 超时秒数"
)
@click.option("--headless", is_flag=True, help="[legacy 模式] 使用 headless 模式执行")
def init_knowledge(
    workspace,
    dry_run,
    yes,
    includes,
    excludes,
    codegraph,
    legacy,
    scan_profile,
    adapter,
    timeout,
    headless,
):
    """初始化项目知识包。

    默认以确定性探测模式扫描项目结构，展示概览并确认范围后生成知识文件。
    使用 --legacy 可切换到旧的 AI CLI 扫描模式。
    """
    ws = Path(workspace or Path.cwd()).resolve()

    if legacy:
        _run_legacy_init(ws, scan_profile, adapter, timeout, dry_run, headless)
        return

    from ..knowledge.detector import detect_project
    from ..knowledge.scope import recommend_scope
    from ..knowledge.wizard import (
        show_project_overview,
        show_file_stats,
        show_recommended_scope,
        show_candidate_domains,
        interactive_confirm,
        show_next_steps,
    )
    from ..knowledge.run_writer import create_run_id, write_run_artifacts
    from ..knowledge.generator import generate_knowledge_files
    from ..knowledge.paths import manifest_path

    console.print("\n[bold cyan]初始化项目知识包[/]")
    console.print(f"  工作区: [dim]{ws}[/]")

    if manifest_path(ws).exists():
        console.print("\n[yellow]已有知识包。[/]")
        console.print("  1) 更新（重新探测并覆盖）")
        console.print("  2) Dry-run（只探测不写入）")
        console.print("  3) 取消")
        choice = click.prompt("选择", type=int, default=1)
        if choice == 2:
            dry_run = True
        elif choice == 3:
            console.print("[yellow]已取消。[/]")
            return

    # Step 1: 探测项目结构
    console.print("\n[1/4] 探测项目结构...")
    detection = detect_project(ws)
    console.print("  [green]done[/]")

    # Step 2: 展示概览
    show_project_overview(detection)
    show_file_stats(detection)

    # Step 3: 生成推荐范围
    scope = recommend_scope(detection)

    # Apply explicit include/exclude overrides
    if includes:
        _apply_includes(scope, includes, detection)
    if excludes:
        _apply_excludes(scope, excludes)

    show_recommended_scope(scope)
    show_candidate_domains(scope)

    # Step 4: 用户确认
    if not yes:
        confirmed = interactive_confirm(detection, scope)
        if confirmed is None:
            console.print("[yellow]已取消。[/]")
            return
        scope = confirmed
    else:
        console.print("\n[dim]--yes 模式，自动接受推荐范围。[/]")

    if dry_run:
        console.print("\n[dim]--dry-run 模式，不写入知识文件。[/]")
        return

    # Step 5: 写入 run artifacts
    run_id = create_run_id()
    console.print(f"\n[2/4] 保存探测记录 ({run_id})...")
    mode = "non-interactive" if yes else "interactive"
    write_run_artifacts(ws, run_id, detection, scope, mode=mode)
    console.print("  [green]done[/]")

    # Step 6: 生成知识文件
    console.print("\n[3/4] 生成项目概览知识文件...")
    created = generate_knowledge_files(ws, detection, scope)
    console.print(f"  [green]{len(created)} 个文件已生成[/]")

    # Step 7: 下一步建议
    show_next_steps(scope)
    console.print(f"\n  知识包位置: [dim]{ws / '.story' / 'knowledge'}[/]")


@project.command("sync-knowledge")
@click.option("-w", "--workspace", default=None, help="工作区目录（默认当前目录）")
def sync_knowledge(workspace):
    """检测知识包是否过期，提示增量更新。"""
    from ..knowledge.paths import manifest_path

    ws = Path(workspace or Path.cwd()).resolve()
    mp = manifest_path(ws)

    if not mp.exists():
        console.print("[yellow]知识包不存在。请先运行 story project init-knowledge[/]")
        raise SystemExit(1)

    console.print("\n[bold cyan]检测知识包状态[/]")
    console.print(f"  工作区: [dim]{ws}[/]")

    try:
        from ..knowledge.stale import check_stale

        result = check_stale(ws)
    except ImportError:
        # stale module may not exist yet (Task 8)
        console.print("[yellow]stale 检测模块尚未实现。[/]")
        return

    if result["stale"]:
        console.print("\n[yellow]知识包已过期[/]")
        console.print(f"  原因: {result['reason']}")
        console.print(f"\n建议运行: [bold]story project init-knowledge -w {ws}[/]")
    else:
        console.print("\n[green]知识包是最新的[/]")
        if result.get("commit"):
            console.print(f"  commit: [dim]{result['commit'][:12]}[/]")


def _run_legacy_init(ws, scan_profile, adapter, timeout, dry_run, headless):
    """Legacy AI CLI scanning mode (pre-08 behavior)."""
    from ..knowledge.scaffold import scaffold_knowledge_dir
    from ..knowledge.bootstrap import render_bootstrap_prompt
    from ..knowledge.validator import validate_knowledge_pack

    console.print("\n[bold cyan]初始化项目知识包 (legacy mode)[/]")
    console.print(f"  工作区: [dim]{ws}[/]")
    console.print(f"  扫描 profile: [dim]{scan_profile}[/]")

    console.print("\n[1/4] 创建目录结构...")
    scaffold_knowledge_dir(ws)
    console.print("  [green]done[/]")

    if dry_run:
        console.print("\n[dim]--dry-run 模式，不执行 AI CLI。目录已创建。[/]")
        return

    available = _detect_available_adapters()
    if adapter:
        if (
            not shutil.which(adapter)
            and not shutil.which(f"{adapter}.cmd")
            and adapter not in available
        ):
            console.print(f"[red]未找到 {adapter} CLI。[/]")
            if available:
                console.print(f"  可用: {', '.join(available)}")
            raise SystemExit(1)
    elif available:
        adapter = available[0]
        console.print(f"\n  检测到 [cyan]{adapter}[/]")
    else:
        console.print(
            "[red]未检测到任何 AI CLI（claude/codex）。请先安装或通过 --adapter 指定。[/]"
        )
        raise SystemExit(1)

    console.print("\n[2/4] 渲染 bootstrap prompt...")
    prompt = render_bootstrap_prompt(ws, scan_profile=scan_profile)
    console.print(f"  prompt 长度: [dim]{len(prompt)} 字符[/]")

    if headless:
        console.print(f"\n[3/4] 执行 {adapter} CLI (headless)...")
        console.print("[dim]等待 AI 生成知识包（可能需要几分钟）...[/]")
        try:
            from ..knowledge.bootstrap import run_bootstrap

            result = run_bootstrap(
                ws, scan_profile=scan_profile, adapter_name=adapter, timeout=timeout
            )
            console.print("  [green]AI CLI 完成[/]")
            if result.get("summary"):
                console.print(f"  摘要: {result['summary']}")
        except FileNotFoundError as e:
            console.print(f"\n[red]生成失败: {e}[/]")
            raise SystemExit(1)
        except Exception as e:
            console.print(f"\n[red]执行出错: {e}[/]")
            raise SystemExit(1)
    else:
        console.print(f"\n[3/3] 启动交互式 {adapter} CLI...")
        from ..knowledge.bootstrap import launch_interactive

        launch_interactive(ws, scan_profile=scan_profile, adapter_name=adapter)
        console.print("  [green]已启动[/]")
        console.print(
            "\n[dim]知识包将在 AI 完成交互式扫描后生成。"
            "完成后可运行 story project sync-knowledge 检查状态。[/]"
        )
        return

    console.print("\n[4/4] 校验知识包产物...")
    errors = validate_knowledge_pack(ws)
    if errors:
        console.print(f"  [yellow]{len(errors)} 个问题:[/]")
        for e in errors:
            console.print(f"    - {e}")
    else:
        console.print("  [green]所有关键产物校验通过[/]")

    console.print("\n[green]知识包初始化完成[/]")
    console.print(f"  位置: [dim]{ws / '.story' / 'knowledge'}[/]")


def _apply_includes(scope, includes, detection):
    """Move explicitly included items into scope.included."""
    for name in includes:
        # Check if it's in excluded
        for svc in scope.excluded:
            if svc.id == name or svc.path == name:
                svc.included = True
                svc.reason = "user included"
                scope.included.append(svc)
                scope.excluded.remove(svc)
                break
        else:
            # Check if it's an undetected path
            console.print(f"[dim]  {name} not in detected scope, ignoring.[/]")


def _apply_excludes(scope, excludes):
    """Move explicitly excluded items out of scope.included."""
    for name in excludes:
        for svc in scope.included:
            if svc.id == name or svc.path == name:
                svc.included = False
                svc.reason = "user excluded"
                scope.excluded.append(svc)
                scope.included.remove(svc)
                break


# ── Workspace Onboarding commands ──


@project.command("inspect")
@click.option("-w", "--workspace", default=None, help="工作区目录（默认当前目录）")
@click.option("--json", "as_json", is_flag=True, help="输出原始 JSON")
def inspect(workspace, as_json):
    """Deterministic scan — 输出 observed facts，不写 confirmed profile。"""
    from ..orchestrator.project_scan import scan_workspace
    from ..orchestrator.project_profile import _to_dict

    ws = Path(workspace or Path.cwd()).resolve()

    profile = scan_workspace(ws)

    if as_json:
        import json as _json

        click.echo(_json.dumps(_to_dict(profile), ensure_ascii=False, indent=2))
        return

    console.print("\n[bold cyan]Project Inspection[/]")
    console.print(f"  Workspace: [dim]{ws}[/]")
    console.print(f"  Type: [cyan]{profile.workspace_type}[/]")
    console.print(f"  Confidence: [cyan]{profile.confidence}[/]")

    if profile.repos:
        console.print(f"\n  Repos ([bold]{len(profile.repos)}[/]):")
        for repo in profile.repos:
            dirty_mark = "[red]*[/]" if repo.dirty else " "
            console.print(
                f"    {dirty_mark} {repo.id} ({repo.repo_type}) "
                f"[dim]{', '.join(repo.languages) or 'unknown lang'}[/]"
            )

    if profile.test_sources:
        console.print(f"\n  Test candidates ([bold]{len(profile.test_sources)}[/]):")
        for ts in profile.test_sources:
            console.print(f"    {ts.command} [dim]({ts.repo_id})[/]")

    if profile.release_profile.scale != "unknown":
        console.print(f"\n  Release scale: [cyan]{profile.release_profile.scale}[/]")

    if profile.facts:
        console.print(f"\n  Facts: [bold]{len(profile.facts)}[/]")


@project.command("onboard")
@click.option("-w", "--workspace", default=None, help="工作区目录（默认当前目录）")
@click.option("--force", is_flag=True, help="强制重新扫描（覆盖已有 profile）")
@click.option("--yes", "-y", is_flag=True, help="非交互模式，自动接受扫描结果")
def onboard(workspace, force, yes):
    """执行 scan → 确认流程 → 写 Project Profile。"""
    from ..orchestrator.project_scan import scan_workspace
    from ..orchestrator.project_profile import (
        load_profile,
        save_profile,
        profile_path,
    )

    ws = Path(workspace or Path.cwd()).resolve()

    existing = load_profile(ws)
    if existing and not force:
        console.print("\n[yellow]Project Profile already exists:[/]")
        console.print(f"  Path: [dim]{profile_path(ws)}[/]")
        console.print(
            f"  Type: {existing.workspace_type}  Repos: {len(existing.repos)}"
        )
        console.print(
            "\n  Use [bold]--force[/] to re-scan, or [bold]story project confirm[/] to edit."
        )
        return

    console.print("\n[bold cyan]Workspace Onboarding[/]")
    console.print(f"  Workspace: [dim]{ws}[/]")
    console.print("\n  Scanning...")

    profile = scan_workspace(ws)

    console.print("\n  Detected:")
    console.print(f"    workspace type: [cyan]{profile.workspace_type}[/]")
    if profile.repos:
        console.print(f"    git repos: [bold]{len(profile.repos)}[/]")
        backend = sum(1 for r in profile.repos if r.repo_type == "backend")
        frontend = sum(1 for r in profile.repos if r.repo_type == "frontend")
        if backend:
            console.print(f"    likely backend repos: {backend}")
        if frontend:
            console.print(f"    likely frontend repos: {frontend}")
    if profile.test_sources:
        console.print(f"    test command candidates: {len(profile.test_sources)}")
    if profile.doc_assets:
        console.print(f"    doc assets: {len(profile.doc_assets)}")

    if yes:
        console.print("\n  [dim]--yes mode, accepting scan results.[/]")
    else:
        console.print("\n  Actions:")
        console.print("    [a] accept and save")
        console.print("    [e] edit before save")
        console.print("    [s] skip for now")
        choice = click.prompt("  Choose", type=str, default="a").strip().lower()

        if choice == "s":
            console.print("[yellow]Skipped. Run `story project onboard` to retry.[/]")
            return
        if choice == "e":
            console.print(
                "[dim]Editing not yet supported in CLI. Use --json output and edit profile.json directly.[/]"
            )

    saved = save_profile(ws, profile)
    console.print(f"\n  [green]Profile saved:[/] [dim]{saved}[/]")


@project.command("confirm")
@click.option("-w", "--workspace", default=None, help="工作区目录（默认当前目录）")
def confirm_profile(workspace):
    """对已有 observed facts 做确认/编辑。"""
    from ..orchestrator.project_profile import load_profile, save_profile

    ws = Path(workspace or Path.cwd()).resolve()
    profile = load_profile(ws)

    if profile is None:
        console.print(
            "[yellow]No Project Profile found. Run `story project onboard` first.[/]"
        )
        raise SystemExit(1)

    console.print("\n[bold cyan]Confirm Project Facts[/]")
    console.print(f"  Workspace: [dim]{ws}[/]")

    unconfirmed = [f for f in profile.facts if not f.confirmed]
    if unconfirmed:
        console.print(f"\n  Unconfirmed facts: [bold]{len(unconfirmed)}[/]")
        for fact in unconfirmed:
            console.print(
                f"    [{fact.confidence}] {fact.type}: {fact.value} "
                f"[dim]({fact.scope})[/]"
            )
    else:
        console.print("\n  [green]All facts confirmed.[/]")

    console.print("\n  [a] accept all  [s] skip")
    choice = click.prompt("  Choose", type=str, default="a").strip().lower()

    if choice == "a":
        for f in profile.facts:
            f.confirmed = True
        for r in profile.repos:
            r.confirmed = True
        saved = save_profile(ws, profile)
        console.print(
            f"  [green]All facts confirmed. Profile saved:[/] [dim]{saved}[/]"
        )
    else:
        console.print("  [yellow]Skipped.[/]")


@project.command("probe")
@click.option("-w", "--workspace", default=None, help="工作区目录（默认当前目录）")
@click.option("--question", default=None, help="Probe 问题")
def probe(workspace, question):
    """受控调用 code agent 只读探查（需要 LLM 配置）。"""
    import os

    if not os.environ.get("STORY_LLM_API_KEY"):
        console.print("[red]LLM not configured. Run `story setup` first.[/]")
        console.print("[dim]inspect and onboard commands work without LLM.[/]")
        raise SystemExit(1)

    ws = Path(workspace or Path.cwd()).resolve()
    q = question or "Find test commands, startup commands, and release signals"

    console.print("\n[bold cyan]Project Intelligence Probe[/]")
    console.print(f"  Workspace: [dim]{ws}[/]")
    console.print(f"  Question: {q}")
    console.print("\n  [yellow]Agent probe is not yet implemented in P0.8.[/]")
    console.print("  Use [bold]story project inspect[/] for deterministic scan.")


@project.command("refresh")
@click.option("-w", "--workspace", default=None, help="工作区目录（默认当前目录）")
def refresh(workspace):
    """对现有 Project Profile 做轻量漂移检查。"""
    from ..orchestrator.project_profile import refresh_profile

    ws = Path(workspace or Path.cwd()).resolve()
    console.print("\n[bold cyan]Story Start Refresh[/]")
    console.print(f"  Workspace: [dim]{ws}[/]")

    report = refresh_profile(ws)

    if report.status == "missing_profile":
        console.print("\n  [yellow]No Project Profile found.[/]")
        console.print("  Run [bold]story project onboard[/] to create one.")
        raise SystemExit(1)

    if report.status == "ok":
        console.print("\n  [green]Profile is up-to-date.[/]")
        return

    console.print("\n  [yellow]Drift detected:[/]")
    for item in report.drift:
        icon = "[red]✗[/]" if item.severity == "error" else "[yellow]![/]"
        console.print(f"    {icon} {item.type}: {item.detail}")

    console.print("\n  [u] update profile  [c] continue once  [b] block story")
    choice = click.prompt("  Choose", type=str, default="c").strip().lower()

    if choice == "u":
        from ..orchestrator.project_scan import scan_workspace
        from ..orchestrator.project_profile import save_profile

        profile = scan_workspace(ws)
        saved = save_profile(ws, profile)
        console.print(f"  [green]Profile updated:[/] [dim]{saved}[/]")
    elif choice == "b":
        console.print("  [red]Story blocked due to profile drift.[/]")
        raise SystemExit(1)
    else:
        console.print("  [dim]Continuing with drift warnings.[/]")


# Workspace Onboarding commands (design-workspace-onboarding-project-profile.md)
from . import project_onboard  # noqa: E402, F401
