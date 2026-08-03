"""story workspace — 业务项目实体管理(11-workspace-entity-design.md Phase 2)。

Workspace = 业务项目(如 HappyCash 授信域),聚合 Repo / wiki 知识 / 旅程 / 集成。
显式 opt-in:不建 Workspace 时行为与今天完全一致(开源零配置路径)。
"""

import json

import click
from rich.console import Console

from ...infra.db import models as db

console = Console()


@click.group()
def workspace():
    """业务项目(Workspace)实体管理。"""
    pass


def _resolve(ident: str) -> dict:
    from ...orchestrator.workspace.workspace_registry import get_workspace

    ws = get_workspace(ident)
    if not ws:
        raise click.ClickException(
            f"Workspace 不存在: {ident}(用 story workspace list 查看)"
        )
    return ws


def _parse_repos(repo_specs: tuple[str, ...]) -> list[tuple[str, str]]:
    """解析 --repo name=path 规格(可多次)。"""
    repos: list[tuple[str, str]] = []
    for spec in repo_specs:
        if "=" not in spec:
            raise click.ClickException(f"--repo 格式应为 name=path,收到: {spec!r}")
        name, path = spec.split("=", 1)
        repos.append((name.strip(), path.strip()))
    return repos


def _print_steps(results: list[dict]) -> None:
    for r in results:
        status = {"done": "[green]done[/]", "failed": "[red]failed[/]"}.get(
            r["status"], r["status"]
        )
        line = f"  {r['step']:<22} {status}"
        if r.get("detail"):
            line += f"  {r['detail']}"
        if r.get("reason"):
            line += f"  [red]{r['reason']}[/]"
        console.print(line)


@workspace.command("create")
@click.argument("name")
@click.option("--slug", default=None, help="kebab-case slug(缺省由 name 派生)")
@click.option(
    "--knowledge-root",
    default=None,
    help="知识根目录(默认 <主工作区>/.story/knowledge,由 repos 推断)",
)
def workspace_create(name: str, slug: str | None, knowledge_root: str | None):
    """创建业务项目实体,如: story workspace create "HappyCash 授信域" --slug hc-credit-domain"""
    from ...orchestrator.workspace.workspace_registry import create_workspace

    ws = create_workspace(name, slug=slug, knowledge_root=knowledge_root)
    console.print(
        f"[green]已创建 Workspace[/] #{ws['id']} {ws['name']} (slug: {ws['slug']})"
    )
    console.print(
        f"  下一步: [bold]story workspace init {ws['slug']} --repo name=path[/]"
    )


@workspace.command("init")
@click.argument("name")
@click.option(
    "--repo",
    "repo_specs",
    multiple=True,
    metavar="NAME=PATH",
    help="注册的仓库(可多次,幂等): --repo hc-credit=D:/hc-all/hc-credit",
)
@click.option(
    "--step",
    default=None,
    help="只跑单步: register_repos/detect_runtime/gen_wiki/register_integrations/init_scenarios",
)
@click.option(
    "--integrations-json",
    default=None,
    help='step 4 集成元数据(§6): \'{"gitlab": {"url": "..."}}\'',
)
def workspace_init(
    name: str,
    repo_specs: tuple[str, ...],
    step: str | None,
    integrations_json: str | None,
):
    """初始化管线 5 步(§3):register_repos → detect_runtime → gen_wiki → register_integrations → init_scenarios。

    每步幂等、失败不阻塞后续、可 --step 单步重跑。
    """
    from ...orchestrator.workspace.workspace_registry import run_init_pipeline

    ws = _resolve(name)
    repos = _parse_repos(repo_specs)
    integrations = None
    if integrations_json:
        try:
            integrations = json.loads(integrations_json)
        except json.JSONDecodeError:
            raise click.ClickException("--integrations-json 不是合法 JSON")

    console.print(f"[bold]Workspace init:[/] {ws['name']} ({ws['slug']})")
    if step:
        console.print(f"  单步重跑: {step}")
    results = run_init_pipeline(
        ws["id"],
        step=step,
        repos=repos,
        integrations_json=integrations,
    )
    _print_steps(results)
    if not step and any(r["status"] == "failed" for r in results):
        console.print(
            "[yellow]部分步骤失败,可用 --step <名> 单独重跑;"
            "哪个 probe 没配就缺哪层,优雅降级不影响其他步骤。[/]"
        )


@workspace.command("list")
def workspace_list():
    """列出所有 Workspace 实体 + 初始化状态。"""
    from ...orchestrator.workspace.workspace_registry import list_workspaces

    workspaces = list_workspaces()
    if not workspaces:
        console.print(
            "[yellow]尚未创建 Workspace。[/] 用 [bold]story workspace create <name>[/] 创建。"
        )
        return
    table_header = f"{'ID':<4} {'slug':<24} {'name':<20} {'repos':<5} {'init_state'}"
    console.print(table_header)
    console.print("-" * 90)
    for ws in workspaces:
        state = json.loads(ws.get("init_state") or "{}")
        state_txt = (
            ", ".join(
                f"{k}={v if isinstance(v, str) else v.get('status', 'failed')}"
                for k, v in sorted(state.items())
            )
            or "未初始化"
        )
        repo_count = len(db.list_projects_by_workspace(ws["id"]))
        console.print(
            f"{ws['id']:<4} {ws['slug']:<24} {ws['name'][:18]:<20} "
            f"{repo_count:<5} {state_txt}"
        )


@workspace.command("show")
@click.argument("name")
def workspace_show(name: str):
    """查看 Workspace 详情:Repos + 运行时事实 + 集成 + 初始化状态。"""
    ws = _resolve(name)
    console.print(f"[bold]{ws['name']}[/] (slug: {ws['slug']}, id: {ws['id']})")
    if ws.get("knowledge_root"):
        console.print(f"  知识根: {ws['knowledge_root']}")
    integrations = json.loads(ws.get("integrations_json") or "{}")
    if integrations:
        console.print(f"  集成: {', '.join(integrations.keys())}")
    state = json.loads(ws.get("init_state") or "{}")
    console.print("  初始化:")
    _print_steps(
        [
            {"step": s, **state.get(s, {"status": "pending"})}
            for s in db.WORKSPACE_INIT_STEPS
        ]
    )
    repos = db.list_projects_by_workspace(ws["id"])
    console.print(f"  Repos ({len(repos)}):")
    for repo in repos:
        facts = db.get_runtime_facts(repo["id"])
        fact_txt = (
            ", ".join(
                f"{f['runtime_type']}={f['runtime_version'] or '?'}" for f in facts
            )
            or "无运行时事实"
        )
        console.print(
            f"    - {repo['name']:<20} [{repo['availability']}] {repo['repo_path']}"
            f"  ({fact_txt})"
        )


@workspace.command("delete")
@click.argument("name")
@click.confirmation_option(prompt="确定删除该 Workspace?下属 Repo 保留(脱离归属)")
def workspace_delete(name: str):
    """删除 Workspace 实体。下属 Repo 的 workspace_id 置 NULL,回到散仓库形态。"""
    from ...orchestrator.workspace.workspace_registry import delete_workspace

    if delete_workspace(name):
        console.print(f"[green]已删除 Workspace: {name}[/]")
    else:
        raise click.ClickException(f"Workspace 不存在: {name}")
