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
@click.option(
    "--scan-profile",
    default="java-spring-microservice",
    help="扫描 profile: java-spring-microservice | frontend-react-umi | python-service",
)
@click.option("--adapter", default=None, help="AI CLI adapter（默认自动检测）")
@click.option(
    "--timeout", default=1800, type=int, help="headless 模式超时秒数（默认 1800）"
)
@click.option("--dry-run", is_flag=True, help="只创建目录结构，不执行 AI CLI")
@click.option(
    "--headless", is_flag=True, help="使用 headless 模式执行（非交互，适合 CI）"
)
def init_knowledge(workspace, scan_profile, adapter, timeout, dry_run, headless):
    """初始化项目知识包。

    扫描项目代码库，生成 .story/knowledge/ 下的知识文件。
    默认以交互模式启动 AI CLI（zellij/新终端），使用 --headless 可非交互执行。
    """
    from ..knowledge.scaffold import scaffold_knowledge_dir
    from ..knowledge.bootstrap import render_bootstrap_prompt
    from ..knowledge.validator import validate_knowledge_pack
    from ..knowledge.paths import manifest_path

    ws = Path(workspace or Path.cwd()).resolve()
    console.print("\n[bold cyan]初始化项目知识包[/]")
    console.print(f"  工作区: [dim]{ws}[/]")
    console.print(f"  扫描 profile: [dim]{scan_profile}[/]")

    if manifest_path(ws).exists():
        if not click.confirm("知识包已存在，是否覆盖？"):
            console.print("[yellow]已取消。[/]")
            return

    # Step 1: 创建目录结构
    console.print("\n[1/4] 创建目录结构...")
    scaffold_knowledge_dir(ws)
    console.print("  [green]done[/]")

    if dry_run:
        console.print("\n[dim]--dry-run 模式，不执行 AI CLI。目录已创建。[/]")
        return

    # 检测可用的 AI CLI
    available = _detect_available_adapters()
    if adapter:
        # 用户指定了 adapter，验证是否存在
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
        if len(available) == 1:
            adapter = available[0]
            console.print(f"\n  检测到 [cyan]{adapter}[/]")
        else:
            adapter = available[0]
            console.print(
                f"\n  检测到多个 AI CLI: {', '.join(f'[cyan]{a}[/]' for a in available)}，"
                f"使用 [cyan]{adapter}[/]"
            )
    else:
        console.print(
            "[red]未检测到任何 AI CLI（claude/codex）。请先安装或通过 --adapter 指定。[/]"
        )
        raise SystemExit(1)

    # Step 2: 渲染 prompt
    console.print("\n[2/4] 渲染 bootstrap prompt...")
    prompt = render_bootstrap_prompt(ws, scan_profile=scan_profile)
    console.print(f"  prompt 长度: [dim]{len(prompt)} 字符[/]")

    if headless:
        # Headless 模式（原有逻辑）
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
        # 交互模式（新默认行为）
        console.print(f"\n[3/4] 启动交互式 {adapter} CLI...")

        if shutil.which("zellij"):
            console.print("  [dim]使用 zellij 会话[/]")
        else:
            console.print("  [dim]使用系统终端[/]")
        from ..knowledge.bootstrap import launch_interactive

        launch_interactive(ws, scan_profile=scan_profile, adapter_name=adapter)
        console.print("  [green]已启动[/]")

    # Step 4: 校验
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
