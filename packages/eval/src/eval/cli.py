"""eval CLI — `eval extract / score / replay / report`。

启动时把 STORY_LLM_* 指向 Go 端点（EVAL_LLM_* 可覆盖,见 judges.configure_llm_env）。
"""

from __future__ import annotations

import json
import logging
import sys

import click


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        stream=sys.stdout,
        encoding="utf-8",
        force=True,
    )


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="DEBUG 日志")
def main(verbose: bool):
    _setup_logging(verbose)
    from .judges import configure_llm_env

    configure_llm_env()


@main.command()
def index():
    """A 源:扫描 hc-all 各仓库 origin/master 的 merge 交付单元。"""
    from .gitindex import run_index

    res = run_index()
    click.echo(f"index 完成: {res['deliveries_total']} 个交付单元")


@main.command(name="tapd-scan")
def tapd_scan():
    """B 源:拉取 TAPD stories（需 TAPD token 有效）。"""
    from .tapdscan import run_tapd_scan

    try:
        res = run_tapd_scan()
        click.echo(f"tapd-scan 完成: stories={res['stories']} commit_seeds={res['commit_seeds']}")
    except RuntimeError as e:
        click.echo(f"tapd-scan 失败: {e}", err=True)


@main.command()
@click.option("--llm", is_flag=True, help="对无种子关联的 delivery 跑 LLM 模糊匹配（需 key）")
@click.option("--llm-limit", type=int, default=500, help="LLM 匹配上限")
def link(llm, llm_limit):
    """三方匹配 → stories_matched.jsonl + 待确认队列 + 覆盖率报告。"""
    from .linker import run_link

    res = run_link(do_llm=llm, llm_limit=llm_limit)
    click.echo(
        f"link 完成: 实体 {res['entities']} / A∩B high+official {res['ab_high']} / "
        f"待确认 {res['pending']}"
    )
    if res["pending"]:
        click.echo("  ⚠ 有待确认项 → dataset/links_pending_review.md,标注后跑 `eval review-apply`")


@main.command(name="link-mine")
@click.option("--window-days", type=int, default=90, help="merge 前后时间窗（默认 90 天）")
@click.option("--auto-threshold", type=float, default=0.8, help="自动关联阈值（默认 0.8）")
@click.option("--pending-threshold", type=float, default=0.5, help="进待确认队列阈值（默认 0.5）")
@click.option("--concurrency", type=int, default=None, help="并发数（默认 EVAL_LLM_CONCURRENCY 或 16）")
@click.option("--limit", type=int, default=None, help="只处理前 N 个未关联 merge（试跑）")
@click.option("--verify", is_flag=True, default=False, help="跑完后自动执行 verify-links 复核")
def link_mine(window_days, auto_threshold, pending_threshold, concurrency, limit, verify):
    """对个人未关联 merge 跑加强版 LLM 关联（owner=赵子豪, ±90 天）。"""
    from .linker import run_link_mine

    res = run_link_mine(
        window_days=window_days,
        auto_threshold=auto_threshold,
        pending_threshold=pending_threshold,
        concurrency=concurrency,
        limit=limit,
    )
    click.echo(
        f"link-mine 完成: 个人未关联 {res['mine_unlinked']} / 负责人候选 story {res['owner_pool']} / "
        f"本次处理 {res.get('processed', '?')} / 自动关联 {res['auto_linked']} / 待确认 {res['pending']}"
    )
    if res["pending"]:
        click.echo("  ⚠ 有待确认项 → dataset/links_pending_review.md")
    if verify:
        click.echo("  → 自动进入 verify-links 复核...")
        from .verify_links import run_verify_links

        vres = run_verify_links(
            env_file="dataset/.env.deepseek",
            concurrency=concurrency or 8,
        )
        click.echo(
            f"verify-links 完成: 总 {vres['total']} / related {vres['by_verdict'].get('related', 0)} / "
            f"unrelated {vres['by_verdict'].get('unrelated', 0)} / uncertain {vres['by_verdict'].get('uncertain', 0)}"
        )
        click.echo(f"  抽样清单: {vres['sample']}")


@main.command(name="verify-links")
@click.option("--dataset-dir", default=None, help="dataset 目录")
@click.option("--results-dir", default=None, help="results 目录")
@click.option("--env-file", default="dataset/.env.deepseek", help="DeepSeek env 文件路径")
@click.option("--concurrency", type=int, default=8, help="并发数（默认 8）")
@click.option("--seed", type=int, default=42, help="抽样随机种子")
@click.option("--sample-each", type=int, default=7, help="每层抽样条数（默认 7）")
def verify_links(dataset_dir, results_dir, env_file, concurrency, seed, sample_each):
    """独立复核 pending + llm_mine_high 关联，输出 verify_links_<date>.jsonl + 抽样清单。"""
    from .verify_links import run_verify_links

    res = run_verify_links(
        dataset_dir=dataset_dir,
        results_dir=results_dir,
        env_file=env_file,
        concurrency=concurrency,
        seed=seed,
        sample_each=sample_each,
    )
    click.echo(
        f"verify-links 完成: 总 {res['total']} / related {res['by_verdict'].get('related', 0)} / "
        f"unrelated {res['by_verdict'].get('unrelated', 0)} / uncertain {res['by_verdict'].get('uncertain', 0)}"
    )
    click.echo(f"输出: {res['output']}")
    click.echo(f"抽样清单: {res['sample']}")


@main.command(name="apply-verify")
@click.argument("sample_path", default="dataset/verify_links_sample_20260805.md")
@click.option("--dataset-dir", default=None, help="dataset 目录")
@click.option("--verify-path", default=None, help="verify_links jsonl（默认最新）")
def apply_verify(sample_path, dataset_dir, verify_path):
    """按人工校准后的抽样清单执行分级：related→accept、unrelated→reject、uncertain→留队列。"""
    from .verify_links import run_apply_verify

    res = run_apply_verify(
        sample_path=sample_path,
        dataset_dir=dataset_dir,
        verify_path=verify_path,
    )
    click.echo(
        f"apply-verify 完成: 总 {res['total']} / accept {res['accepted']} / "
        f"reject {res['rejected']} / 留队列 {res['kept_uncertain']}"
    )
    click.echo(f"stories_matched: {res['stories_matched']}")
    click.echo(f"pending: {res['pending_review']}")


@main.command(name="review-apply")
@click.argument("path", default="dataset/links_pending_review.md")
def review_apply(path):
    """应用人工确认结果（accept:xxx）进 link_confirmations.jsonl。"""
    from .linker import review_apply as _apply

    res = _apply(path)
    click.echo(f"review-apply: {res['applied']} 条确认写入 {res['file']}")


@main.command(name="backfill-human")
@click.argument("sample_path", default="dataset/verify_links_sample_20260805.md")
@click.option("--dataset-dir", default=None, help="dataset 目录")
def backfill_human(sample_path, dataset_dir):
    """把人工校准过的 (merge, tapd) 对回写为 stories_matched 的 human_confirmed 标记。"""
    from .verify_links import backfill_human_confirmed

    res = backfill_human_confirmed(sample_path=sample_path, dataset_dir=dataset_dir)
    click.echo(f"backfill-human 完成: 标记 {res['marked']} 条 deliveries（sample {res['sample_keys']} 键）")


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
        f"完成: 实体 {summary['entities_total']} / 入选 {summary['qualified']} "
        f"(A∩B {summary['qualified_ab']} + C {summary['qualified_c']}) / core {summary['core']}"
    )
    for e in summary["errors"]:
        click.echo(f"  ! {e}", err=True)


@main.command()
@click.option("--dataset-dir", default=None)
@click.option("--results-dir", default=None)
@click.option("--limit", type=int, default=None, help="只评前 N 个（调试）")
@click.option("--seed", type=int, default=42)
@click.option("--force", is_flag=True, help="忽略 partial 文件，强制重新打分")
@click.option("--concurrency", type=int, default=None, help="并发数（默认 EVAL_LLM_CONCURRENCY 或 4）")
def score(dataset_dir, results_dir, limit, seed, force, concurrency):
    """对 core 集全量跑 SpecScore+PlanScore,生成 baseline 报告。"""
    from .baseline import run_baseline

    res = run_baseline(
        dataset_dir, results_dir, limit=limit, seed=seed, force=force, concurrency=concurrency
    )
    click.echo(f"baseline 完成: {res['count']} 个 story, {res['json']}")
    click.echo(f"自洽性(分差≤1比例): {res['consistency']['diff_le_1_ratio']:.1%}")
    for e in res["errors"]:
        click.echo(f"  ! {e}", err=True)


@main.command()
@click.option("--dataset-dir", default=None)
@click.option("--results-dir", default=None)
@click.option("--limit", type=int, default=None, help="只评前 N 个 merge（试跑/分批）")
@click.option("--author", "authors", multiple=True, help="按 author 过滤（可多次）")
@click.option("--branch-pattern", "branch_patterns", multiple=True, help="按分支通配过滤（可多次）")
@click.option("--mine", is_flag=True, help="只跑个人交付（zhaozihao/赵子豪 + feature/ice/* + feature/zzh/*）")
def scan_all(dataset_dir, results_dir, limit, authors, branch_patterns, mine):
    """对全量 merge 逐个评分（Conformance+Delivery / MergeSummary+Delivery）。"""
    from .scanall import run_scan_all

    res = run_scan_all(
        limit=limit,
        results_dir=results_dir,
        authors=list(authors) if authors else None,
        branch_patterns=list(branch_patterns) if branch_patterns else None,
        mine=mine,
    )
    click.echo(f"scan-all: 共 {res['total']},本次新增 {res['scored_now']},报告 {res['report']}")
    for e in res["errors"][:10]:
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


@main.command(name="ui-replay")
@click.option("--serve-url", default="http://localhost:8180", help="serve 地址")
@click.option("--results-dir", default=None)
@click.option("--only", default=None, help="只跑单个 story_key")
def ui_replay(serve_url, results_dir, only):
    """UI-driven eval(差分 path B)— 走 serve API 跑 gold story(测调度层)。"""
    from .ui_replay import run_ui_replay

    res = run_ui_replay(serve_url, results_dir, only=only)
    click.echo(f"UI 回放完成: 共 {res['count']},产出 spec {res['ok']},失败 {len(res['failed'])}")
    for f in res["failed"]:
        click.echo(f"  ! {f['story_key']}: spawn={f.get('spawn_triggered')} paused={f.get('paused')} {str(f.get('error',''))[:60]}")


@main.command(name="ui-full")
@click.option("--serve-url", default="http://localhost:8180", help="serve 地址")
@click.option("--results-dir", default=None)
@click.option("--only", default=None, help="只跑单个 story_key")
def ui_full(serve_url, results_dir, only):
    """UI 全流程 eval —— design→implement→verify→done,自动续推 lifecycle confirm-gate。"""
    from .ui_replay import run_ui_full_lifecycle

    res = run_ui_full_lifecycle(serve_url, results_dir, only=only)
    click.echo(f"全流程完成: 共 {res['count']},终态 completed {res['completed']}")
    for s in res["stories"]:
        click.echo(f"  - {s['story_key']}: final={s.get('final_status')}@{s.get('final_stage')} done={s.get('completed_stages')} advances={s.get('advances')} ({s.get('elapsed_s')}s)")


@main.command(name="diff")
@click.option("--results-dir", default=None)
def diff(results_dir):
    """差分 path A (in-process replay) vs path B (ui_replay)— 量化 serve 调度影响。"""
    from .ui_replay import run_diff

    res = run_diff(results_dir)
    click.echo(f"差分报告: {res['md']}")


@main.command(name="human-matrix")
@click.option("--story-home", default=None, help="story-lifecycle STORY_HOME（默认读环境变量）")
def human_matrix(story_home):
    """C 线:人判 vs 机判混淆矩阵（judge_feedback ⋈ orchestrator_decision）。

    口径:机判 approve + 人判 disagree = 漏拦;机判 reject/escalate + 人判 disagree = 误拦。
    迭代 4 设计 §5。空表不崩（输出 0 行）。
    """
    import os as _os

    if story_home:
        _os.environ["STORY_HOME"] = story_home
    from story_lifecycle.infra.db import models as _db
    from story_lifecycle.infra.db import feedback

    _db.init_db()
    m = feedback.confusion_matrix()
    click.echo(f"反馈总数: {m['total']}（agree {m['agree']} / disagree {m['disagree']}）")
    click.echo(f"漏拦(机判 approve + 人判 disagree): {m['missed_block']}")
    click.echo(f"误拦(机判 reject/escalate + 人判 disagree): {m['false_block']}")
    if m["rows"]:
        click.echo("明细:")
        for r in m["rows"]:
            click.echo(
                f"  #{r['id']} {r['story_key']} dec={r['decision_id']} "
                f"机判={r['machine_decision']} 人判={r['human_decision']} "
                f"note={r['note'] or '-'} @{r['created_at']}"
            )


@main.command(name="ref-fetch")
@click.option("--priority", is_flag=True, help="只抓优先批(link-only ∩ stories_matched)")
@click.option("--tapd-id", multiple=True, help="只抓指定 tapd_id（可多次）")
@click.option("--limit", type=int, default=None, help="只处理前 N 个 (tapd_id, url)（试跑）")
@click.option("--route", multiple=True, help="只抓指定 fetcher 路线（tapd_api/curl/webbridge/login_required,可多次）")
@click.option("--retry", is_flag=True, help="连确定性错误(empty_content/login_wall 等)也强制重试")
@click.option("--dry-run", is_flag=True, help="只列出待抓清单,不抓")
@click.option("--stats", is_flag=True, help="只输出索引统计")
def ref_fetch(priority, tapd_id, limit, route, retry, dry_run, stats):
    """抓 link-only 需求链接正文 → dataset/story_refs/<tapd_id>.md + 索引。"""
    from .ref_fetch import index_stats, run_fetch

    if stats:
        click.echo(json.dumps(index_stats(), ensure_ascii=False, indent=2))
        return
    res = run_fetch(
        priority_only=priority,
        tapd_ids=list(tapd_id) if tapd_id else None,
        limit=limit,
        dry_run=dry_run,
        routes=list(route) if route else None,
        retry=retry,
    )
    if res.get("dry_run"):
        click.echo(f"dry-run: 待抓 {res['todo']} 个 (tapd_id, url),涉及 {res['stories']} 条需求")
        return
    click.echo(
        f"ref-fetch 完成: 本次 {res['fetched']} 个链接,分布 {res['by_status']},"
        f"索引 {res['index']}"
    )
    for e in res["errors"][:10]:
        click.echo(f"  ! {e}", err=True)


@main.command(name="ref-stats")
def ref_stats():
    """ref-fetch 索引统计（fetcher × status 分布 + 覆盖）。"""
    from .ref_fetch import index_stats

    click.echo(json.dumps(index_stats(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
