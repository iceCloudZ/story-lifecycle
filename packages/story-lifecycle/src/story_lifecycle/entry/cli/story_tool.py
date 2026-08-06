"""story-tool CLI — code agent 用的成果物落地入口(普通 CLI,非 MCP)。

DESIGN-artifact-driven-stage-completion §4.4 / STEP 1 子任务 1.3。

注入 code agent 的 prompt,让它在干完活后调 `story tool declare` 落成果物,
替掉旧的"写 done.json 自报"协议。子命令:
  - workspace:打印当前 story 上下文(story_key/stage/workspace)。
  - declare <doc_type> <path>:成果物落地(原子写+版本化+done.json兼容视图+触发编排器)。
  - todo:打印当前 stage 缺哪些 artifacts。

context 从环境读(STORY_KEY/STORY_STAGE/STORY_WORKSPACE,planner spawn 时注入)。
纯逻辑在 artifact_declare.py;本文件只做 CLI 装配(click + 打印)。
"""

from __future__ import annotations

import os
import sys

import click
from rich.console import Console

console = Console()


@click.group()
def tool():
    """story-tool — code agent 成果物落地入口。

    由编排器在 spawn code agent 时注入 prompt 调用。code agent 干完活调
    `story tool declare <doc_type> <path>` 落成果物,替旧 done.json 自报协议。
    """


@tool.command()
def workspace():
    """打印当前 story 上下文(story_key/stage/workspace)。

    code agent 先调这个确认自己在 story 上下文里(环境变量由编排器注入)。
    """
    sk = os.environ.get("STORY_KEY", "")
    st = os.environ.get("STORY_STAGE", "")
    ws = os.environ.get("STORY_WORKSPACE", "") or str(os.getcwd())
    if not sk or not st:
        console.print(
            "[yellow]⚠ 不在 story 上下文里(STORY_KEY/STORY_STAGE 未设)。[/]\n"
            "正常情况编排器 spawn 时会注入这些环境变量。"
        )
    console.print(f"  Story Key : [cyan]{sk or '(unset)'}[/]")
    console.print(f"  Stage     : [cyan]{st or '(unset)'}[/]")
    console.print(f"  Workspace : [cyan]{ws}[/]")


@tool.command()
@click.argument("doc_type")
@click.argument("path")
@click.option(
    "--summary", "-s", default="", help="一句话摘要(进 done.json + story_doc)"
)
@click.option(
    "--content",
    "content",
    default=None,
    help="直接给内容(原子写到 path);不给则登记已存在的 path 文件。",
)
@click.option(
    "--files-changed",
    "files_changed",
    default="",
    help="逗号分隔的本成果物涉及文件清单(进 done.json files_changed)",
)
def declare(doc_type, path, summary, content, files_changed):
    """声明一个成果物:原子写 + 版本化 + done.json 兼容视图 + 触发编排器感知。

    \b
    Examples:
      story tool declare spec story/spec.md --summary "登录方案"
      story tool declare test_report story/test-report.md
      story tool declare spec story/spec.md --content "# 方案\\n..."
    """
    from ...orchestrator.engine.artifact_declare import declare_artifact

    fc = (
        [s.strip() for s in files_changed.split(",") if s.strip()]
        if files_changed
        else None
    )
    try:
        result = declare_artifact(
            doc_type=doc_type,
            path=path,
            content=content,
            summary=summary,
            files_changed=fc,
        )
    except (ValueError, FileNotFoundError) as exc:
        console.print(f"[red]✗ declare 失败:[/] {exc}")
        raise SystemExit(1)

    color = "green" if result["atomic"] else "yellow"
    console.print(
        f"[{color}]✓ declared {result['doc_type']}[/] "
        f"-> [cyan]{result['path']}[/]\n"
        f"  atomic={'是' if result['atomic'] else '否(降级直接写)'}  "
        f"version=v{result['version']}  "
        f"done_view={result['done_view'] or '(失败)'}"
    )


@tool.command()
def todo():
    """打印当前 stage 还缺哪些 artifacts 要 declare。

    code agent 调这个自查:哪些 artifact 已 declare(算完成)、哪些还没。
    完成判据归一化为 ``artifact_declared`` event(1068018 事故修复)——
    写了文件但没 declare 不算完成,得调 ``story tool declare`` 才算。
    """
    from ...orchestrator.engine.artifact_check import (
        build_evidence_candidates,
        check_artifacts_landed,
    )
    from ...orchestrator.engine.artifact_check import _ARTIFACT_TO_DOC_TYPE
    from ...orchestrator.engine.profile_loader import resolve_profile

    from ...infra.db import models as db

    sk = os.environ.get("STORY_KEY", "")
    st = os.environ.get("STORY_STAGE", "")
    ws = os.environ.get("STORY_WORKSPACE", "") or str(os.getcwd())
    if not sk or not st:
        console.print("[yellow]⚠ 不在 story 上下文里(STORY_KEY/STORY_STAGE 未设)。[/]")
        raise SystemExit(1)

    story = db.get_story(sk)
    profile_name = (story or {}).get("profile", "minimal")
    try:
        rp = resolve_profile(profile_name)
    except Exception as exc:
        console.print(f"[red]✗ 加载 profile {profile_name} 失败:[/] {exc}")
        raise SystemExit(1)

    stage_cfg = rp.stage(st)
    artifacts = stage_cfg.artifacts
    if not artifacts:
        console.print(
            f"[yellow]stage {st} 未声明 artifacts(profile={profile_name})。[/]"
        )
        return

    console.print(f"Stage [cyan]{st}[/] artifacts (workspace={ws}):")
    # 查本轮最新 declare event(doc_type → 已 declare)
    declared_docs: set[str] = set()
    try:
        payload = db.get_latest_declare(sk, st, since_version=-1)
        if payload:
            declared_docs.add(payload.get("doc_type", ""))
    except Exception:  # noqa: BLE001
        pass

    _title = (story or {}).get("title", "") or ""
    _ev = build_evidence_candidates(artifacts, ws, sk, _title)
    missing, landed = check_artifacts_landed(artifacts, ws, evidence_candidates=_ev)
    n_pending = 0
    for art in artifacts:
        if art == "git":
            done = art in landed
            mark = "[green]✓[/]" if done else "[red]✗[/]"
            tip = "" if done else "（需要未提交的代码改动）"
            console.print(f"  {mark} {art}{tip}")
            if not done:
                n_pending += 1
            continue
        doc_type = _ARTIFACT_TO_DOC_TYPE.get(art, "")
        if doc_type and doc_type in declared_docs:
            console.print(f"  [green]✓[/] {art}（已 declare）")
        elif art in landed:
            # 文件写了但没 declare → 提醒调 declare
            console.print(
                f"  [yellow]⚠[/] {art}（文件已写，但还没 declare → 调 `story tool declare {doc_type} {art}`）"
            )
            n_pending += 1
        else:
            console.print(f"  [red]✗[/] {art}（还没写，也没 declare）")
            n_pending += 1
    if n_pending == 0:
        console.print("[green]全部 declare 完成，stage 可推进。[/]")
    else:
        console.print(f"[yellow]还差 {n_pending} 个未 declare，继续干。[/]")
        raise SystemExit(2)


def main():
    """独立入口:`python -m story_lifecycle.entry.cli.story_tool` 或 story-tool 脚本。"""
    # Force UTF-8 on Windows (GBK can't encode Chinese)
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    tool()


if __name__ == "__main__":
    main()
