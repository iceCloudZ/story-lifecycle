"""story-tool declare 逻辑 — 成果物落地入口(code agent 产出落地)。

DESIGN-artifact-driven-stage-completion §4.4 / STEP 1 子任务 1.3 + 1.5。

code agent 干完一段活,不写 done.json(那是不可信自报),而是调 story-tool declare:
把成果物文件原子写到约定位置 + 版本化进 story_doc + 写一份 done.json 兼容视图(给 miner
维持旧 link 逻辑,直到 P7 切换)+ 触发编排器感知(log_event)。

**红线**:原子写与存在检查同批做 —— declare 单次调用内完成原子写;planner 的
check_artifacts_landed 查的就是这里原子写出的文件,无半成品竞态窗口(设计 §2.3)。

**miner 双写兼容**(红线):declare 同时写一份 .story/done/<key>/<stage>.json 兼容视图,
payload 含 story_ingest 要的 spec_path/summary/files_changed/stage。story_ingest.py 零改。

本模块只放纯逻辑(可测);CLI 入口(entry/cli/story_tool.py)和编排器内部调用都走这里。
DB / 文件操作是副作用,但集中在 declare_artifact 一个函数,易测(mock db)。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ...infra.atomic_write import atomic_write
# ENV_STORY_* 的真相源在 story_paths(spawn 侧 build_story_spawn_env 与本模块
# _story_ctx 共用同一组名字,避免 spawner 漏注入或写错名字 —— 见 story_paths.build_story_spawn_env)。
from ...infra.story_paths import (
    ENV_STORY_KEY,
    ENV_STORY_STAGE,
    ENV_STORY_TITLE,
    ENV_STORY_WORKSPACE,
)

log = logging.getLogger("story-lifecycle.story_tool")


# doc_type → done.json payload 字段映射(miner story_ingest 读这些字段)。
# story_ingest 主要读 design.json 的 spec_path/complexity/summary/affected_repos,
# 以及 done/<stage>.json 的存在性(取 mtime)。这里按 doc_type 填合理字段。
_DOC_DONE_FIELDS = {
    "spec": ("spec_path", "complexity"),
    "research": ("research_path",),
    "plan": ("plan_path",),
    "test_report": ("test_report_path",),
    "delivery": ("delivery_path",),
    "bugfix-report": ("bugfix_report_path",),
}


def _resolve_workspace(explicit: str = "") -> str:
    """workspace 来源:显式 > 环境变量 > cwd。"""
    import os

    return explicit or os.environ.get(ENV_STORY_WORKSPACE, "") or str(Path.cwd())


def _story_ctx(explicit: dict | None = None) -> dict:
    """从环境读 story_key/stage/workspace/title。explicit 覆盖环境。"""
    import os

    e = explicit or {}
    return {
        "story_key": e.get("story_key") or os.environ.get(ENV_STORY_KEY, ""),
        "stage": e.get("stage") or os.environ.get(ENV_STORY_STAGE, ""),
        "workspace": e.get("workspace") or _resolve_workspace(),
        "title": e.get("title") or os.environ.get(ENV_STORY_TITLE, ""),
    }


def declare_artifact(
    doc_type: str,
    path: str,
    *,
    content: str | None = None,
    summary: str = "",
    files_changed: list[str] | None = None,
    ctx: dict | None = None,
    db_module=None,
) -> dict:
    """声明一个成果物:原子写 + 版本化 story_doc + done.json 兼容视图 + 触发编排器感知。

    Args:
        doc_type: 成果物类型(spec/research/plan/test_report/delivery/bugfix-report/自定义)。
        path: 成果物文件路径(相对 workspace 或绝对)。code agent 已写好的文件就指它;
              若给 content 则原子写到 path(覆盖)。
        content: 可选 — 给了就原子写 path(替 code agent 写);None 则只登记已存在文件。
        summary: 一句话摘要(进 done.json 兼容视图的 summary 字段 + story_doc change_reason)。
        files_changed: 本成果物涉及的文件清单(进 done.json files_changed,供 miner + story_document)。
        ctx: {story_key, stage, workspace, title};None 则从环境读。
        db_module: 注入 db models(测试用);None 则延迟 import。

    Returns:
        {atomic, path, doc_type, story_key, stage, version, done_view, event}
        供 CLI 打印 + 测试断言。

    单次调用内完成四件事(红线:同批做):
      1. 原子写文件(若 content 给了)/ 校验文件存在(若只登记)
      2. upsert_story_doc 版本化(版本+1)
      3. 写 done.json 兼容视图(miner 双写兼容,1.5)
      4. log_event artifact_declared(触发编排器感知)
    """
    from ...infra.paths import stage_done_file_rel
    from ...infra.story_paths import story_doc_path

    c = _story_ctx(ctx)
    story_key = c["story_key"]
    stage = c["stage"]
    workspace = c["workspace"]
    title = c["title"]
    if not story_key or not stage:
        raise ValueError(
            f"declare 缺 story 上下文:story_key={story_key!r} stage={stage!r}"
            f"(设环境 {ENV_STORY_KEY}/{ENV_STORY_STAGE} 或传 ctx)"
        )

    if db_module is None:
        from ...infra.db import models as db_module

    ws_path = Path(workspace)
    # path 解析:绝对直接用;相对则相对 workspace。
    artifact_path = Path(path)
    if not artifact_path.is_absolute():
        artifact_path = ws_path / artifact_path

    # ---- 1. 原子写(若给 content)或校验存在 ----
    atomic_info = {"atomic": True, "path": str(artifact_path)}
    if content is not None:
        atomic_info = atomic_write(artifact_path, content)
    elif not artifact_path.exists():
        raise FileNotFoundError(
            f"declare 指向的成果物文件不存在: {artifact_path}(code agent 应先写好)"
        )

    # 读文件内容进 story_doc(content 参数优先,否则读已存在文件)。
    doc_content = content
    if doc_content is None:
        try:
            doc_content = artifact_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log.warning("read artifact for story_doc failed (non-fatal): %s", exc)
            doc_content = ""

    # ---- 2. story_doc 版本化(版本+1) ----
    version = 0
    try:
        version = db_module.upsert_story_doc(
            story_key,
            doc_type,
            doc_content,
            change_reason=summary or f"AI {stage} 阶段产出({doc_type})",
            author="ai",
        )
        # local_path 指向 evidence 目录下的标准 .md 缓存(让 doc_sync 能找到)。
        try:
            evidence_md = story_doc_path(workspace, story_key, doc_type, title)
            db_module.set_story_doc_local_path(story_key, doc_type, str(evidence_md))
        except Exception:  # noqa: BLE001 — local_path 是 informational
            pass
    except Exception:  # noqa: BLE001 — 版本化 best-effort,不阻塞成果物落地
        log.exception(
            "upsert_story_doc failed for %s/%s (non-fatal)", story_key, doc_type
        )

    # ---- 3. done.json 兼容视图(miner 双写兼容,1.5 红线) ----
    done_view = _write_done_compat_view(
        story_key=story_key,
        stage=stage,
        workspace=workspace,
        doc_type=doc_type,
        artifact_path=artifact_path,
        summary=summary,
        files_changed=files_changed,
        stage_done_file_rel=stage_done_file_rel,
    )

    # ---- 4. log_event 触发编排器感知 ----
    event_payload = {
        "doc_type": doc_type,
        "path": str(artifact_path),
        "atomic": atomic_info["atomic"],
        "version": version,
        "done_view": done_view,
        "summary": summary,
        "files_changed": files_changed or [],
    }
    try:
        db_module.log_event(story_key, stage, "artifact_declared", event_payload)
    except Exception:  # noqa: BLE001 — 事件 best-effort
        log.exception("log_event artifact_declared failed (non-fatal)")

    log.info(
        "[%s/%s] declared %s -> %s (atomic=%s, v%d, done_view=%s)",
        story_key,
        stage,
        doc_type,
        artifact_path,
        atomic_info["atomic"],
        version,
        done_view,
    )
    return {
        "atomic": atomic_info["atomic"],
        "path": str(artifact_path),
        "doc_type": doc_type,
        "story_key": story_key,
        "stage": stage,
        "version": version,
        "done_view": done_view,
        "event": "artifact_declared",
    }


def _write_done_compat_view(
    *,
    story_key: str,
    stage: str,
    workspace: str,
    doc_type: str,
    artifact_path: Path,
    summary: str,
    files_changed: list[str] | None,
    stage_done_file_rel,
) -> str | None:
    """写一份 .story/done/<key>/<stage>.json 兼容视图给 miner(双写兼容,1.5)。

    miner 的 story_ingest.py 读这些文件:取 stage mtime(文件存在性)+ design.json 的
    spec_path/complexity/summary。本函数把 declare 的成果物信息落成兼容格式,让
    story_ingest 零改动继续工作。直到 P7 切换。

    payload 字段:
      stage, status=done, summary, files_changed,
      + 按 doc_type 填的 spec_path/test_report_path 等(story_ingest 显式读这些)。
    """
    payload = {
        "stage": stage,
        "status": "done",
        "summary": summary or f"{stage} 阶段产出({doc_type})",
        "files_changed": list(files_changed or [str(artifact_path)]),
    }
    # 按 doc_type 补 story_ingest 显式读的字段。
    for field_name in _DOC_DONE_FIELDS.get(doc_type, ()):
        payload[field_name] = str(artifact_path)
    # spec 类成果物额外补 complexity(story_ingest design.json 读)。
    if doc_type == "spec":
        payload.setdefault("complexity", "unknown")

    try:
        done_rel = stage_done_file_rel(story_key, stage)
        done_path = Path(workspace) / done_rel
        atomic_write(done_path, json.dumps(payload, ensure_ascii=False, indent=2))
        return str(done_path)
    except Exception:  # noqa: BLE001 — 兼容视图 best-effort(成果物已落地,这是双写兼容层)
        log.exception(
            "write done.json compat view failed (non-fatal) for %s/%s", story_key, stage
        )
        return None
