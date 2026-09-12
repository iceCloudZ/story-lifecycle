"""routers/release_review — 发版窗口评估(DESIGN-v1-work-agent WP-E §7)。

``GET /api/trains/{train}/release-review``:按 ``release_train`` 捞 story 集,
纯读聚合出每条 story 的发版证据面 + train 级 rollup,供 pre-release-review
skill / ReleaseTrainBoard 消费研判。本路由只做**证据存在性**分级,不做内容
判断(回滚可行性结论归 skill)。

train 登记(写侧)不在这里 —— 复用既有
``PUT /api/story/{key}/release-train``(routers/lifecycle.py),读面是
``GET /api/story`` 的 ``releaseTrain`` 字段。

Response shape(snake_case,与 digest sections 同风格)::

    {
      "train": "app-1.2.33",
      "stories": [
        {
          "story_key": "tapd-123",
          "title": "...",
          "lifecycle_state": "测试",
          "status": "active",
          "gates": [ ...check_deliverables 条目(key/label/satisfied/...) ],
          "gate": {from,to,required,all_satisfied} | null,   # 当前状态推进 gate
          "unmet_deliverables": ["测试报告", ...],            # 未满足的成果物中文名
          "projects": [{"project_id","branch","base_branch","base_commit",
                        "worktree_state","worktree_path"}],
          "mrs": [{"external_id","url","source_branch","target_branch",
                   "delivery_state","review_state","evidence_ref"}],
          "ddl": {"path","size"} | null,   # workspace story 目录 ddl.sql 扫描(best-effort)
          "docs": [{"kind","ref","summary"}],  # registered documents(ddl/impact/spec/test_plan)
          "patrol": {"run_id","result","failed_count","started_at","run_scope",
                     "inherited_from_train"} | null,   # 本 story 最近一轮,无则借用 train 轮
          "rollback_risk": {"level": "high"|"medium"|"low",
                            "reason": "...",           # 中文,证据存在性依据
                            "pending_skill_review": bool}  # 关键词命中 → true
        }, ...
      ],
      "rollup": {
        "total": N,
        "risk_counts": {"high": n, "medium": n, "low": n},
        "unmet_gates": [{"story_key", "missing": [...]}],
        "mrs_not_merged": [{"story_key","external_id","url","delivery_state",
                            "evidence_ref"}],
        "latest_patrol": {"run_id","story_key","result","failed_count",
                          "started_at","run_scope"} | null
      }
    }
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, HTTPException

from ....infra.db import models as db

log = logging.getLogger("story-lifecycle.api-release-review")

router = APIRouter(tags=["release-review"])

# 回滚风险关键词(证据存在性规则,不做内容判断):命中 → medium 待 skill 研判。
_RISK_KEYWORDS = re.compile(r"nacos|配置|api[\s_-]?bump|ddl", re.IGNORECASE)
# medium 档检索的 registered document kinds(spec 的别名 design 一并认,
# 与 deliverables gate 的 resolve_doc_type_aliases 同理)。
_MEDIUM_DOC_KINDS = ("impact", "spec", "design")
# docs 字段透传的 kinds(ddl/test_plan/impact + spec 系)。
_DOCS_KINDS = frozenset({"ddl", "test_plan", "impact", "spec", "design"})
# 已落地的 delivery 终态(与 sourcing/deliverables 的 landed 语义一致)。
_LANDED_DELIVERY_STATES = {"merged", "abandoned"}


def _scan_workspace_ddl(story: dict) -> dict | None:
    """best-effort 扫 workspace story 目录的 ``ddl.sql``。

    约定路径 ``<workspace>/story/<id>-<slug>/ddl.sql``(story_evidence_dir),
    兜底 glob ``<story 根>/.*<story 数字 id>*/ddl.sql``。非空文件 →
    ``{"path","size"}``;找不到 → None。全程吞异常 —— 扫描失败绝不 500。
    """
    try:
        workspace = (story.get("workspace") or "").strip()
        if not workspace:
            return None
        from ....infra.story_paths import (
            story_evidence_dir,
            story_numeric_id,
        )

        sid = story_numeric_id(story.get("story_key") or "")
        evidence_dir = story_evidence_dir(
            workspace, story.get("story_key") or "", story.get("title") or ""
        )
        candidates = [evidence_dir / "ddl.sql"]
        try:
            candidates.extend(evidence_dir.parent.glob(f"*{sid}*/ddl.sql"))
        except Exception:  # noqa: BLE001 — glob 失败不阻塞 canonical 路径检查
            log.debug("ddl glob scan failed (non-fatal)", exc_info=True)
        for cand in candidates:
            try:
                if cand.is_file() and cand.stat().st_size > 0:
                    return {"path": str(cand), "size": cand.stat().st_size}
            except OSError:
                continue
        return None
    except Exception:  # noqa: BLE001 — best-effort,绝不 500
        log.debug("ddl scan failed (non-fatal)", exc_info=True)
        return None


def _registered_docs(story_key: str) -> list[dict]:
    """registered documents(story_document)中与发版评估相关的 kinds。"""
    try:
        docs = db.get_story_documents(story_key)
    except Exception:  # noqa: BLE001 — 纯读聚合,单点失败降级为空
        log.debug("get_story_documents failed (non-fatal)", exc_info=True)
        return []
    return [
        {"kind": d.get("kind") or "", "ref": d.get("ref") or "", "summary": d.get("summary") or ""}
        for d in docs
        if (d.get("kind") or "") in _DOCS_KINDS
    ]


def _classify_rollback_risk(
    ddl: dict | None, docs: list[dict]
) -> dict:
    """回滚风险分级 —— **证据存在性规则**,不做内容判断。

    - high:ddl.sql 非空文件,或已登记 kind=ddl 文档(动过表结构,回滚必查)
    - medium:impact/spec 登记(ref+summary)命中 nacos|配置|api bump|ddl 关键词
      → pending_skill_review=true,真判断归 pre-release-review skill
    - low:其余(纯代码)
    """
    if ddl is not None or any(d["kind"] == "ddl" for d in docs):
        return {
            "level": "high",
            "reason": "存在 DDL 证据(ddl.sql 非空或已登记 ddl 文档)",
            "pending_skill_review": False,
        }
    for d in docs:
        if d["kind"] not in _MEDIUM_DOC_KINDS:
            continue
        blob = f"{d['ref']} {d['summary']}"
        if _RISK_KEYWORDS.search(blob):
            return {
                "level": "medium",
                "reason": f"登记文档 {d['kind']} 命中风险关键词(待 skill 研判)",
                "pending_skill_review": True,
            }
    return {
        "level": "low",
        "reason": "无 DDL / 风险关键词证据",
        "pending_skill_review": False,
    }


def _fail_count(run_id: int) -> int:
    """一轮 run 的 FAIL 项数(与 patrol overview 的 SUM(CASE) 同口径)。"""
    with db._db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM patrol_run_item "
            "WHERE run_id = ? AND result = 'FAIL'",
            (run_id,),
        ).fetchone()
    return row["c"] if row else 0


def _latest_train_scoped_run(train: str) -> dict | None:
    """run_scope == "train:<train>" 的最近一轮(与 routers/patrol.py 的 scope 约定同源)。"""
    with db._db() as conn:
        row = conn.execute(
            "SELECT * FROM patrol_run WHERE run_scope = ? ORDER BY id DESC LIMIT 1",
            (f"train:{train}",),
        ).fetchone()
    if not row:
        return None
    r = dict(row)
    fails = _fail_count(r["id"])
    return {
        "run_id": r["id"],
        "story_key": r["story_key"],
        "result": "FAIL" if fails > 0 else "PASS",
        "failed_count": fails,
        "started_at": r.get("started_at") or "",
        "run_scope": r.get("run_scope") or "",
    }


def _story_patrol(story_key: str, train: str) -> dict | None:
    """该 story 的最近 patrol run;本 story 没跑过则借用 train 轮(标记 inherited)。"""
    try:
        runs = db.list_patrol_runs(story_key, limit=1)
    except Exception:  # noqa: BLE001
        log.debug("list_patrol_runs failed (non-fatal)", exc_info=True)
        runs = []
    if runs:
        r = runs[0]
        return {
            "run_id": r["id"],
            "result": r.get("result"),
            "failed_count": sum(1 for it in r.get("items") or [] if it.get("result") == "FAIL"),
            "started_at": r.get("started_at") or "",
            "run_scope": r.get("run_scope") or "",
            "inherited_from_train": False,
        }
    inherited = _latest_train_scoped_run(train)
    if inherited:
        inherited["inherited_from_train"] = True
    return inherited


def _train_rollup_patrol(train: str, story_keys: list[str]) -> dict | None:
    """train 级最近巡检结论:train 轮优先,否则该 train 各 story 最近一轮。"""
    scoped = _latest_train_scoped_run(train)
    if scoped:
        return scoped
    if not story_keys:
        return None
    placeholders = ",".join("?" * len(story_keys))
    with db._db() as conn:
        row = conn.execute(
            f"SELECT * FROM patrol_run WHERE story_key IN ({placeholders}) "
            f"ORDER BY id DESC LIMIT 1",
            story_keys,
        ).fetchone()
    if not row:
        return None
    r = dict(row)
    fails = _fail_count(r["id"])
    return {
        "run_id": r["id"],
        "story_key": r["story_key"],
        "result": "FAIL" if fails > 0 else "PASS",
        "failed_count": fails,
        "started_at": r.get("started_at") or "",
        "run_scope": r.get("run_scope") or "",
    }


@router.get("/api/trains/{train}/release-review")
def train_release_review(train: str):
    """发版窗口评估:train 聚合 + 回滚风险数据面(WP-E)。响应 shape 见模块 docstring。"""
    train = (train or "").strip()
    if not train:
        raise HTTPException(status_code=400, detail="train 不能为空")

    with db._db() as conn:
        stories = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM story WHERE release_train = ? AND deleted_at IS NULL "
                "ORDER BY story_key",
                (train,),
            ).fetchall()
        ]
    if not stories:
        raise HTTPException(
            status_code=404,
            detail=f"班车 {train} 下没有 story(先 PUT /api/story/{{key}}/release-train 登记)",
        )

    # 复用 GET /deliverables 同一套检查(check_deliverables + gate_for_current_state)
    from ....sourcing.deliverables import check_deliverables, gate_for_current_state

    entries: list[dict] = []
    unmet_gates: list[dict] = []
    mrs_not_merged: list[dict] = []
    risk_counts = {"high": 0, "medium": 0, "low": 0}

    for s in stories:
        key = s["story_key"]
        items = check_deliverables(key)
        gate = gate_for_current_state(key)
        unmet = [d["label"] for d in items if not d.get("satisfied")]
        if unmet:
            unmet_gates.append({"story_key": key, "missing": unmet})

        mrs = [
            {
                "external_id": a.get("external_id") or "",
                "url": a.get("url") or "",
                "source_branch": a.get("source_branch") or "",
                "target_branch": a.get("target_branch") or "",
                "delivery_state": a.get("delivery_state") or "",
                "review_state": a.get("review_state") or "",
                "evidence_ref": a.get("evidence_ref") or "",
            }
            for a in db.get_story_delivery_artifacts(key)
        ]
        for mr in mrs:
            if mr["delivery_state"] not in _LANDED_DELIVERY_STATES:
                mrs_not_merged.append(
                    {
                        "story_key": key,
                        "external_id": mr["external_id"],
                        "url": mr["url"],
                        "delivery_state": mr["delivery_state"],
                        "evidence_ref": mr["evidence_ref"],
                    }
                )

        projects = [
            {
                "project_id": p.get("project_id"),
                "branch": p.get("branch") or "",
                "base_branch": p.get("base_branch") or "",
                "base_commit": p.get("base_commit") or "",
                "worktree_state": p.get("worktree_state") or "",
                "worktree_path": p.get("worktree_path"),
            }
            for p in db.get_story_projects(key)
        ]

        docs = _registered_docs(key)
        ddl = _scan_workspace_ddl(s)
        risk = _classify_rollback_risk(ddl, docs)
        risk_counts[risk["level"]] = risk_counts.get(risk["level"], 0) + 1

        entries.append(
            {
                "story_key": key,
                "title": s.get("title") or "",
                "lifecycle_state": s.get("lifecycle_state") or "",
                "status": s.get("status") or "",
                "gates": items,
                "gate": gate,
                "unmet_deliverables": unmet,
                "projects": projects,
                "mrs": mrs,
                "ddl": ddl,
                "docs": docs,
                "patrol": _story_patrol(key, train),
                "rollback_risk": risk,
            }
        )

    return {
        "train": train,
        "stories": entries,
        "rollup": {
            "total": len(entries),
            "risk_counts": risk_counts,
            "unmet_gates": unmet_gates,
            "mrs_not_merged": mrs_not_merged,
            "latest_patrol": _train_rollup_patrol(train, [s["story_key"] for s in stories]),
        },
    }
