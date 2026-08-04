"""eval CLI — `eval extract / score / replay / report`。

启动时把 STORY_LLM_* 指向 Go 端点（EVAL_LLM_* 可覆盖,见 judges.configure_llm_env）。
"""

from __future__ import annotations

import logging
import sys

import click


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        stream=sys.stdout,
        encoding="utf-8",
    )


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="DEBUG 日志")
def main(verbose: bool):
    _setup_logging(verbose)
    from .judges import configure_llm_env

    configure_llm_env()


@main.command()
@click.option("--db", default=None, help="生产 story.db 路径（默认 ~/.story-lifecycle/story.db）")
@click.option("--dataset-dir", default=None, help="dataset 输出目录")
@click.option("--workspace", default=None, help="只抽取该 workspace")
@click.option("--story-key", default=None, help="只抽取该 story_key")
@click.option("--force", is_flag=True, help="覆盖已存在目录")
def extract(db, dataset_dir, workspace, story_key, force):
    """从生产 DB + 证据目录抽取 gold 数据集。"""
    from .dataset import DEFAULT_DB, extract

    summary = extract(
        db_path=db or DEFAULT_DB,
        dataset_dir=dataset_dir,
        workspace=workspace,
        story_key=story_key,
        force=force,
    )
    click.echo(
        f"完成: 候选 {summary['total_completed']} / 有证据 {summary['with_evidence']} / "
        f"入选 {summary['qualified']} (+fs {summary.get('fs_only', 0)}) / core {summary['core']}"
    )
    for e in summary["errors"]:
        click.echo(f"  ! {e}", err=True)


@main.command()
@click.option("--dataset-dir", default=None)
@click.option("--results-dir", default=None)
@click.option("--limit", type=int, default=None, help="只评前 N 个（调试）")
@click.option("--seed", type=int, default=42)
def score(dataset_dir, results_dir, limit, seed):
    """对 core 集全量跑 SpecScore+PlanScore,生成 baseline 报告。"""
    from .baseline import run_baseline

    res = run_baseline(dataset_dir, results_dir, limit=limit, seed=seed)
    click.echo(f"baseline 完成: {res['count']} 个 story, {res['json']}")
    click.echo(f"自洽性(分差≤1比例): {res['consistency']['diff_le_1_ratio']:.1%}")
    for e in res["errors"]:
        click.echo(f"  ! {e}", err=True)


@main.command()
@click.option("--results-dir", default=None)
@click.option("--only", default=None, help="只回放单个 story_key")
def replay(results_dir, only):
    """驱动真实 opencode 回放 gold story（需先确认时机 + opencode CLI）。"""
    from .replay import run_replay

    res = run_replay(results_dir, only=only)
    click.echo(f"回放完成: 共 {res['count']},成功 {res['ok']},失败 {len(res['failed'])}")
    for f in res["failed"]:
        click.echo(f"  ! {f['story_key']}: {f.get('error')}")


@main.command()
@click.option("--dataset-dir", default=None)
@click.option("--results-dir", default=None)
def report(dataset_dir, results_dir):
    """回放产出过 judges,生成回归报告（vs gold baseline / 上次回放）。"""
    from .report import run_report

    res = run_report(dataset_dir, results_dir)
    click.echo(f"回归报告: {res['md']}")


if __name__ == "__main__":
    main()
