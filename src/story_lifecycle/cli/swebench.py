"""`story swebench` — SWE-bench benchmark runner 命令组。"""

import json
import click
from pathlib import Path
from rich.console import Console

from ..benchmarks.swebench import (
    load_instances_jsonl,
    RunStore,
    checkout_instance,
    prepare_instance,
    export_predictions,
)
from ..db.models import init_db

console = Console()


@click.group(name="swebench")
def swebench_group():
    """SWE-bench benchmark runner。"""
    init_db()


@swebench_group.command()
@click.option(
    "--instances",
    type=click.Path(exists=True, path_type=Path),
    required=True,
    help="本地 JSONL instance 文件路径",
)
@click.option("--run-id", required=True, help="Run ID")
@click.option(
    "--workspace-root",
    type=click.Path(path_type=Path),
    default=Path(".story-runs/swebench"),
    help="Run 根目录",
)
@click.option("--agent", default="claude", help="Agent 名称")
@click.option(
    "--budget",
    default="smoke",
    type=click.Choice(["smoke", "standard", "leaderboard"]),
    help="预算档位",
)
@click.option("--limit", type=int, default=None, help="最大 instance 数量")
@click.option(
    "--cache-root",
    type=click.Path(path_type=Path),
    default=Path.home() / ".cache" / "story-lifecycle" / "swebench" / "repos",
    help="Clone cache 根目录",
)
@click.option(
    "--no-checkout", is_flag=True, help="跳过 git checkout（仅创建 manifest 和 Story）"
)
@click.option(
    "--repo-url-template",
    default="https://github.com/{repo}.git",
    help="Repo clone URL 模板，{repo} 替换为 owner/name。"
    "Gitee 示例: https://gitee.com/mirrors/{name}.git",
)
@click.option(
    "--mode",
    type=click.Choice(["benchmark", "development"]),
    default="benchmark",
    help="执行模式",
)
@click.option(
    "--gate-policy",
    type=click.Choice(["auto_fail", "auto_retry", "auto_accept_risk", "wait_confirm"]),
    default=None,
    help="Gate 策略（默认随 mode 自动选择）",
)
def prepare(
    instances,
    run_id,
    workspace_root,
    agent,
    budget,
    limit,
    cache_root,
    no_checkout,
    repo_url_template,
    mode,
    gate_policy,
):
    """准备 SWE-bench run：加载 instances、checkout repos、创建 Stories。"""
    if gate_policy is None:
        gate_policy = "wait_confirm" if mode == "development" else "auto_fail"

    console.print(f"[bold]加载 instances:[/] {instances}")
    inst_list = load_instances_jsonl(instances, limit=limit)
    console.print(f"  共 {len(inst_list)} 个 instances")

    store = RunStore(workspace_root)
    store.create_run(
        run_id=run_id,
        instances=inst_list,
        agent=agent,
        budget=budget,
        mode=mode,
        gate_policy=gate_policy,
    )
    console.print(f"  Run 目录: [dim]{workspace_root / run_id}[/]")

    prepared = 0
    failed = 0
    for inst in inst_list:
        ws = workspace_root / run_id / inst.instance_id

        if not no_checkout:
            result = checkout_instance(
                inst, ws, cache_root, repo_url_template=repo_url_template
            )
            if result["status"] == "checkout_failed":
                console.print(
                    f"  [red]✗[/] {inst.instance_id}: checkout 失败 — {result['error'][:60]}"
                )
                store.update_instance(
                    run_id,
                    inst.instance_id,
                    status="checkout_failed",
                    failure_type="checkout_failure",
                    error=result["error"],
                )
                failed += 1
                continue

        result = prepare_instance(inst, workspace=ws, run_id=run_id, agent=agent)
        console.print(f"  [green]✓[/] {inst.instance_id}")
        prepared += 1

    console.print(f"\n[bold]准备完成:[/] {prepared} 成功, {failed} 失败")
    console.print(f"Manifest: [dim]{workspace_root / run_id / 'manifest.json'}[/]")


@swebench_group.command()
@click.option("--run-id", required=True, help="Run ID")
@click.option(
    "--workspace-root",
    type=click.Path(path_type=Path),
    default=Path(".story-runs/swebench"),
    help="Run 根目录",
)
def solve(run_id, workspace_root):
    """启动所有 prepared instances 的 Story 执行。"""
    from ..orchestrator.graph import run_story

    store = RunStore(workspace_root)
    manifest = store.load_manifest(run_id)

    started = 0
    for entry in manifest["instances"]:
        if entry["status"] != "prepared":
            continue
        instance_id = entry["instance_id"]
        try:
            store.update_instance(run_id, instance_id, status="running")
            console.print(f"  [green]→[/] {instance_id} 执行中...")
            run_story(entry["story_key"])
            # Check actual result from DB
            from ..db import models as db

            story = db.get_story(entry["story_key"])
            final_status = (story or {}).get("status", "unknown")
            last_error = (story or {}).get("last_error")
            if final_status == "completed":
                console.print(f"  [green]✓[/] {instance_id}: completed")
                store.update_instance(run_id, instance_id, status="completed")
                started += 1
            else:
                label = f"{final_status}"
                if last_error:
                    label += f" — {last_error[:80]}"
                console.print(f"  [yellow]![/] {instance_id}: {label}")
                store.update_instance(
                    run_id,
                    instance_id,
                    status=final_status,
                    error=last_error,
                )
        except Exception as e:
            console.print(f"  [red]✗[/] {instance_id}: {e}")
            store.update_instance(
                run_id,
                instance_id,
                status="failed",
                failure_type="execution_failure",
                error=str(e),
            )

    console.print(f"\n[bold]已完成 {started} 个 instances[/]")


@swebench_group.command()
@click.option("--run-id", required=True, help="Run ID")
@click.option(
    "--workspace-root",
    type=click.Path(path_type=Path),
    default=Path(".story-runs/swebench"),
    help="Run 根目录",
)
@click.option("--agent", default="claude", help="Agent 名称")
def export(run_id, workspace_root, agent):
    """导出 predictions.jsonl。"""
    store = RunStore(workspace_root)
    rows = export_predictions(store, run_id, agent=agent)
    pred_path = workspace_root / run_id / "predictions.jsonl"
    console.print(f"[bold]导出完成:[/] {len(rows)} predictions")
    console.print(f"  文件: [dim]{pred_path}[/]")

    manifest = store.load_manifest(run_id)
    noisy = [e for e in manifest["instances"] if e.get("noise_tags")]
    if noisy:
        console.print(f"\n[yellow]⚠ {len(noisy)} 个 patches 触发噪音检测:[/]")
        for e in noisy:
            console.print(f"  - {e['instance_id']}: {', '.join(e['noise_tags'])}")


@swebench_group.command("summarize")
@click.option("--run-id", required=True, help="Run ID")
@click.option(
    "--workspace-root",
    type=click.Path(path_type=Path),
    default=Path(".story-runs/swebench"),
    help="Run 根目录",
)
def summarize_cmd(run_id, workspace_root):
    """生成 run summary。"""
    store = RunStore(workspace_root)
    manifest = store.load_manifest(run_id)

    total = len(manifest["instances"])
    by_status: dict[str, int] = {}
    by_failure: dict[str, int] = {}

    for entry in manifest["instances"]:
        status = entry.get("status", "unknown")
        by_status[status] = by_status.get(status, 0) + 1
        ft = entry.get("failure_type")
        if ft:
            by_failure[ft] = by_failure.get(ft, 0) + 1

    predictions_path = workspace_root / run_id / "predictions.jsonl"
    pred_count = 0
    if predictions_path.exists():
        for line in predictions_path.read_text(encoding="utf-8").strip().split("\n"):
            if line.strip():
                pred_count += 1

    summary = {
        "run_id": run_id,
        "total": total,
        **by_status,
        "predictions": pred_count,
        "failures": by_failure,
    }

    summary_path = workspace_root / run_id / "summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    console.print(f"[bold]Run Summary: {run_id}[/]")
    console.print(f"  Total: {total}")
    for status, count in by_status.items():
        console.print(f"  {status}: {count}")
    console.print(f"  Predictions: {pred_count}")
    if by_failure:
        console.print("  [red]Failures:[/]")
        for ft, count in by_failure.items():
            console.print(f"    {ft}: {count}")
    console.print(f"\n  [dim]{summary_path}[/]")


@swebench_group.command()
@click.option(
    "--instances", type=click.Path(exists=True, path_type=Path), required=True
)
@click.option("--run-id", required=True, help="Run ID")
@click.option(
    "--workspace-root",
    type=click.Path(path_type=Path),
    default=Path(".story-runs/swebench"),
    help="Run 根目录",
)
@click.option("--agent", default="claude")
@click.option(
    "--budget", default="smoke", type=click.Choice(["smoke", "standard", "leaderboard"])
)
@click.option("--limit", type=int, default=None)
@click.option("--no-start", is_flag=True, help="只 prepare 不 solve")
@click.option("--no-checkout", is_flag=True, help="跳过 git checkout")
@click.option(
    "--evaluate", is_flag=True, default=False, help="调用官方 harness（P0 不支持）"
)
@click.option(
    "--repo-url-template",
    default="https://github.com/{repo}.git",
    help="Repo clone URL 模板，同 prepare",
)
@click.option(
    "--mode",
    type=click.Choice(["benchmark", "development"]),
    default="benchmark",
    help="执行模式",
)
@click.option(
    "--gate-policy",
    type=click.Choice(["auto_fail", "auto_retry", "auto_accept_risk", "wait_confirm"]),
    default=None,
    help="Gate 策略（默认随 mode 自动选择）",
)
@click.pass_context
def run(
    ctx,
    instances,
    run_id,
    workspace_root,
    agent,
    budget,
    limit,
    no_start,
    no_checkout,
    evaluate,
    repo_url_template,
    mode,
    gate_policy,
):
    """完整 run：prepare -> solve -> export -> summarize。"""
    if evaluate:
        console.print(
            "[red]Error:[/] --evaluate requires official SWE-bench harness (P1). "
            "Use `story swebench export` + `story swebench eval` manually."
        )
        raise SystemExit(1)
    ctx.invoke(
        prepare,
        instances=instances,
        run_id=run_id,
        workspace_root=workspace_root,
        agent=agent,
        budget=budget,
        limit=limit,
        cache_root=Path.home() / ".cache" / "story-lifecycle" / "swebench" / "repos",
        no_checkout=no_checkout,
        repo_url_template=repo_url_template,
        mode=mode,
        gate_policy=gate_policy,
    )

    if not no_start:
        ctx.invoke(solve, run_id=run_id, workspace_root=workspace_root)

    ctx.invoke(export, run_id=run_id, workspace_root=workspace_root, agent=agent)

    ctx.invoke(summarize_cmd, run_id=run_id, workspace_root=workspace_root)
