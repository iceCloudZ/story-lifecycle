"""story-tool CLI — code agent 用的成果物落地入口(普通 CLI,非 MCP)。

DESIGN-artifact-driven-stage-completion §4.4 / STEP 1 子任务 1.3。

注入 code agent 的 prompt,让它在干完活后调 `story tool declare` 落成果物,
替掉旧的"写 done.json 自报"协议。子命令:
  - workspace:打印当前 story 上下文(story_key/stage/workspace)。
  - declare <doc_type> <path>:成果物落地(原子写+版本化+done.json兼容视图+触发编排器)。
  - todo:打印当前 stage 缺哪些 artifacts。
  - context:打印当前 story 的任务简报(PRD 摘要/接手说明/本阶段任务)。resume
    或上下文丢失时自服务拉全貌;resume seed 会指回这里。

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


@tool.command()
def context():
    """打印当前 story 的任务简报(PRD 摘要 / 接手说明 / 本阶段任务)。

    resume 或上下文丢失时,code agent 调这个重新拉全貌(不用人重新黏贴)。
    纯读聚合,无副作用。本 stage 要 declare 哪些成果物另见 ``story tool todo``。
    """
    from ...infra.db import models as db

    sk = os.environ.get("STORY_KEY", "")
    st = os.environ.get("STORY_STAGE", "")
    ws = os.environ.get("STORY_WORKSPACE", "") or str(os.getcwd())
    if not sk or not st:
        console.print("[yellow]⚠ 不在 story 上下文里(STORY_KEY/STORY_STAGE 未设)。[/]")
        raise SystemExit(1)

    story = db.get_story(sk)
    if not story:
        console.print(f"[red]✗ 找不到 story {sk}[/]")
        raise SystemExit(1)

    # stage 优先用 env(STORY_STAGE = 当前执行阶段),fallback story.current_stage
    stage = st or (story.get("current_stage") or "")
    console.print(_build_context_brief(story, stage))


def _build_context_brief(story: dict, stage: str) -> str:
    """组装任务简报 markdown(纯函数:读 story 行 + stage,无 env 依赖,便于测试)。

    story: ``db.get_story()`` 返回的整行(含 context_json / current_stage /
    title / profile / workspace)。stage: 当前执行阶段(调用方优先传 env
    STORY_STAGE,fallback story.current_stage)。

    best-effort:context_json 解析失败 / PRD 读不到 / _agent_actions 缺失 都降级
    输出已有部分,绝不抛(镜像 todo 的健壮性)。PRD 摘要复用
    ``planner._read_prd_snippet`` 与规划 LLM 同源。
    """
    import json

    from ...orchestrator.engine.planner import _read_prd_snippet

    title = story.get("title", "") or "(无标题)"
    sk = story.get("story_key", "") or ""
    profile = story.get("profile", "") or "(无)"
    workspace = story.get("workspace", "") or "(无)"

    ctx: dict = {}
    try:
        ctx = json.loads(story.get("context_json") or "{}")
    except (ValueError, TypeError):
        ctx = {}

    seed_context = (ctx.get("seed_context") or "").strip()
    prd_path = ctx.get("prd_path") or ""

    # _agent_actions 是 list of dicts(每项 action/stage/focus/task_actions/...);
    # 兼容历史 dict 形态。按 stage 匹配取当前 action。
    actions = ctx.get("_agent_actions") or []
    action: dict = {}
    if isinstance(actions, list):
        action = next(
            (a for a in actions if isinstance(a, dict) and a.get("stage") == stage),
            {},
        )
    elif isinstance(actions, dict):
        matched = actions.get(stage)
        action = matched if isinstance(matched, dict) else {}
    task_actions = action.get("task_actions") or []
    focus = (action.get("focus") or "").strip()

    lines: list[str] = ["# 任务简报", ""]
    lines.append("### Story 信息")
    lines.append(f"- Story Key: `{sk}`")
    lines.append(f"- 标题: {title}")
    lines.append(f"- 当前阶段: **{stage or '(未知)'}**")
    lines.append(f"- Profile: {profile}")
    lines.append(f"- Workspace: `{workspace}`")
    lines.append("")

    lines.append("### PRD 摘要")
    prd_text = _read_prd_snippet(prd_path)
    if prd_text:
        lines.append(prd_text)
    elif prd_path:
        lines.append(f"(PRD 路径 `{prd_path}` 暂时读不到——直接读该文件看完整需求)")
    else:
        lines.append("(无 PRD 路径)")
    lines.append("")

    if seed_context:
        lines.append("### 已有工作(接手)")
        lines.append(seed_context)
        lines.append("")

    lines.append(f"### 本阶段任务({stage or '?'})")
    if focus:
        lines.append(f"**要点**: {focus}")
    if task_actions:
        for i, ta in enumerate(task_actions, 1):
            if isinstance(ta, dict):
                key = ta.get("key") or ta.get("name") or ta.get("action") or ""
                desc = ta.get("description") or ta.get("desc") or ""
                lines.append(f"{i}. {key}{' — ' + desc if desc else ''}")
            else:
                lines.append(f"{i}. {ta}")
    if not focus and not task_actions:
        lines.append("(本阶段无结构化任务清单——见 prompt 文件或规划摘要)")
    lines.append("")

    lines.append("---")
    lines.append(
        "提示:本 stage 要 declare 哪些成果物 → `story tool todo`;"
        "落成果物 → `story tool declare <doc_type> <path>`。"
    )
    return "\n".join(lines)


def main():
    """独立入口:`python -m story_lifecycle.entry.cli.story_tool` 或 story-tool 脚本。"""
    # Force UTF-8 on Windows (GBK can't encode Chinese)
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    tool()


if __name__ == "__main__":
    main()
