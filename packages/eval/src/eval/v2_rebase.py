"""snapshot v2 Go 重基（§1）— 与 v1 同键集合重评。

- 集合: ``snapshot_20260805/merge_scores.jsonl`` 全部键 (repo, merge_hash)
- judge: story_lifecycle.orchestrator.evaluation.conformance.check_conformance
  （Go 端点 + 当前 prompt，迭代 1 移植版）
- 参照物优先级: evidence spec > evidence PRD > story_refs > TAPD 描述
- human_confirmed / human_recalibrated 从 v1 stories_matched.jsonl 原样携带
- 无关联 merge: judges.judge_merge_summary + judge_delivery（同走 Go）
- 断点续跑: v2 merge_scores.jsonl 中无 error 的键跳过
- LLM 调用日志: hook LLMClient._request 记录每次请求的 base_url，供「全程 Go」验收
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("eval.v2_rebase")

PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent
SNAP_V1 = PACKAGE_ROOT / "dataset" / "snapshot_20260805"
SNAP_V2 = PACKAGE_ROOT / "dataset" / "snapshot_v2_20260806"
RESULTS = PACKAGE_ROOT / "results"
REFS_DIR = SNAP_V2 / "refs"
OUT_PATH = SNAP_V2 / "merge_scores.jsonl"
CALLS_LOG = RESULTS / "v2_rebase_calls.log"

SYS_PATH_STORY_LIFECYCLE = str(PACKAGE_ROOT.parent / "story-lifecycle" / "src")

_calls_lock = threading.Lock()


def _append_call(line: str) -> None:
    with _calls_lock:
        with open(CALLS_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def hook_llm_calls() -> None:
    """hook LLMClient._request，把每次请求的 base_url 记入 CALLS_LOG。"""
    from story_lifecycle.infra.llm_client import LLMClient

    orig = LLMClient._request

    def _logged(self, body: dict, *, timeout: int = 90):
        url = f"{self.base_url}/chat/completions" if not self.base_url.endswith("/chat/completions") else self.base_url
        model = (body.get("model") or "")
        ts = _dt.datetime.now().isoformat(timespec="seconds")
        try:
            resp = orig(self, body, timeout=timeout)
            _append_call(f"{ts} OK  {url} model={model}")
            return resp
        except Exception as e:
            _append_call(f"{ts} ERR {url} model={model} err={str(e)[:120]}")
            raise

    LLMClient._request = _logged  # type: ignore[method-assign]


def load_v1_rows() -> list[dict]:
    rows: dict[tuple[str, str], dict] = {}
    for line in (SNAP_V1 / "merge_scores.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            rows[(rec["repo"], rec["merge_hash"])] = rec
    return list(rows.values())


def load_v1_deliveries() -> dict[tuple[str, str], dict]:
    """v1 快照 deliveries.jsonl → (repo, merge_hash) → delivery（commits/diffstat 回查用）。"""
    out: dict[tuple[str, str], dict] = {}
    p = SNAP_V1 / "deliveries.jsonl"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                out[(rec["repo"], rec["merge_hash"])] = rec
    return out


def load_v2_done() -> dict[tuple[str, str], dict]:
    done: dict[tuple[str, str], dict] = {}
    if OUT_PATH.exists():
        for line in OUT_PATH.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                done[(rec["repo"], rec["merge_hash"])] = rec
    return done


def load_match_index() -> tuple[dict[tuple[str, str], dict], dict[str, dict]]:
    """v1 stories_matched.jsonl → (repo, merge_hash) → {entity, confidence, human_confirmed,
    human_recalibrated};tapd_id → tapd story（v1 tapd_stories.jsonl）。"""
    idx: dict[tuple[str, str], dict] = {}
    tapd: dict[str, dict] = {}
    p = SNAP_V1 / "stories_matched.jsonl"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            ent = json.loads(line)
            for dl in ent.get("deliveries", []):
                conf = dl.get("confidence", "")
                if conf not in ("high", "official", "confirmed"):
                    continue
                key = (dl["repo"], dl["merge_hash"])
                if key not in idx or idx[key].get("confidence") == "confirmed":
                    idx[key] = {
                        "entity": ent,
                        "confidence": conf,
                        "human_confirmed": bool(dl.get("human_confirmed")),
                        "human_recalibrated": bool(ent.get("human_recalibrated")) or bool(dl.get("human_recalibrated")),
                    }
    tp = SNAP_V1 / "tapd_stories.jsonl"
    if tp.exists():
        for line in tp.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                tapd[rec["tapd_id"]] = rec
    return idx, tapd


def _strip_html(text: str) -> str:
    import re

    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()


def reference_for(entity: dict, tapd: dict[str, dict]) -> tuple[str, str]:
    """参照物优先级: evidence spec > evidence PRD > story_refs > TAPD 描述。返回 (text, type)。

    evidence 读取快照优先（snapshot_v2_20260806/evidence/），活目录兜底。
    """
    from eval.evidence_snapshot import read_evidence_reference
    from eval.ref_fetch import is_link_only

    text, t = read_evidence_reference(entity.get("evidence_dir") or "")
    if text:
        return text, t
    tid = entity.get("tapd_id") or ""
    rec = tapd.get(tid) or {}
    desc = rec.get("description") or ""
    if is_link_only(desc):
        ref_p = SNAP_V1 / "story_refs" / f"{tid}.md"
        if ref_p.exists():
            text = ref_p.read_text(encoding="utf-8", errors="replace").strip()
            if len(text) >= 100:
                return text[:120_000], "story_refs"
    text = _strip_html(desc)
    return (text[:40_000], "tapd") if text else ("", "")


def _write_ref_text(tid: str, ref_type: str, text: str) -> str:
    """参照物落临时文件，供 check_conformance 的 spec_path 读。"""
    REFS_DIR.mkdir(parents=True, exist_ok=True)
    p = REFS_DIR / f"{tid}.md"
    p.write_text(text, encoding="utf-8")
    return str(p)


def score_linked(
    v1row: dict, linked: dict, tapd: dict[str, dict], diff_text: str
) -> dict:
    """有关联: conformance（Go + 当前 prompt）+ severity_findings（迭代 3 G2）。

    severity_findings 复用核心包 inject_conformance_findings（双阈值：alignment<=2
    或 coverage<=2 → HIGH），不另写规则（单一事实源）。
    """
    from story_lifecycle.orchestrator.evaluation.conformance import (
        ConformanceResult,
        check_conformance,
        inject_conformance_findings,
    )

    ent = linked["entity"]
    tid = ent.get("tapd_id") or ""
    ref, ref_type = reference_for(ent, tapd)
    if not ref:
        return {"error": "no reference"}

    spec_path = _write_ref_text(tid, ref_type, ref)
    res = check_conformance(
        story_key=f"tapd-{tid}",
        workspace=str(SNAP_V1),
        spec_path=spec_path,
        diff_text=diff_text[:120_000],
    )
    severity_findings = inject_conformance_findings(
        ConformanceResult(
            alignment=res.alignment,
            coverage=res.coverage,
            scope_drift=res.scope_drift,
            findings=res.findings,
            reference_type=res.reference_type,
            summary=res.summary,
        )
    )
    return {
        "conformance_score": {
            "alignment": res.alignment,
            "coverage": res.coverage,
            "scope_drift": res.scope_drift,
            "reference_type": ref_type,
            "reference_type_prompt": res.reference_type,
            "findings": res.findings,
            "summary": res.summary,
            "severity_findings": severity_findings,
        }
    }


def score_unlinked(v1row: dict, deliveries: dict[tuple[str, str], dict]) -> dict:
    """无关联: merge summary + delivery（同走 Go 端点）。"""
    from eval import judges

    d = deliveries.get((v1row["repo"], v1row["merge_hash"]), {})
    commits = d.get("commits") or []
    out: dict = {}
    try:
        ms = judges.judge_merge_summary(
            commits, v1row.get("repo", ""), v1row.get("branch", ""), v1row.get("diffstat") or {}
        )
        out["merge_summary"] = ms.model_dump()
    except Exception as e:  # noqa: BLE001
        out["merge_summary_error"] = str(e)
    try:
        ds = judges.judge_delivery(commits, v1row.get("repo", ""), v1row.get("branch", ""))
        out["delivery_score"] = ds.model_dump()
    except Exception as e:  # noqa: BLE001
        out["delivery_error"] = str(e)
    return out


def main() -> dict:
    if sys.path[0] != SYS_PATH_STORY_LIFECYCLE:
        sys.path.insert(0, SYS_PATH_STORY_LIFECYCLE)
    import eval.judges as judges
    from eval import scanall

    judges.configure_llm_env()
    base_url = os.environ.get("STORY_LLM_BASE_URL", "")
    model = os.environ.get("STORY_LLM_MODEL", "")
    log.info("judge 端点: %s / model=%s", base_url, model)
    hook_llm_calls()
    _append_call(f"# start {_dt.datetime.now().isoformat(timespec='seconds')} base_url={base_url} model={model}")

    v1_rows = load_v1_rows()
    done = load_v2_done()
    idx, tapd = load_match_index()
    deliveries = load_v1_deliveries()
    todo = [r for r in v1_rows if (r["repo"], r["merge_hash"]) not in done]
    log.info("v2 重基: v1 共 %d 条,已完成 %d,待评 %d", len(v1_rows), len(done), len(todo))

    concurrency = int(os.environ.get("EVAL_LLM_CONCURRENCY", "1"))
    judges.reset_token_usage()
    errors: list[str] = []
    written = 0
    t0 = time.monotonic()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    def _score_one(row: dict) -> dict:
        out = {
            "repo": row["repo"],
            "merge_hash": row["merge_hash"],
            "branch": row.get("branch", ""),
            "merged_at": row.get("merged_at", ""),
            "author": row.get("author", ""),
            "diffstat": row.get("diffstat", {}),
            "ownership": row.get("ownership", ""),
            "truncated": row.get("truncated", False),
            "scored_at": _dt.datetime.now().isoformat(timespec="seconds"),
        }
        linked = idx.get((row["repo"], row["merge_hash"]))
        if linked:
            ent = linked["entity"]
            out["tapd_id"] = ent.get("tapd_id") or ""
            out["story_key"] = ent.get("story_key") or ""
            out["confidence"] = linked["confidence"]
            out["human_confirmed"] = linked["human_confirmed"]
            out["human_recalibrated"] = linked["human_recalibrated"]
            diff_text, truncated = scanall._diff_text(row["repo"], row["merge_hash"])
            out["truncated"] = truncated
            try:
                out.update(score_linked(row, linked, tapd, diff_text))
            except Exception as e:  # noqa: BLE001
                out["error"] = f"conformance: {e}"
                log.warning("conformance 失败 %s:%s: %s", row["repo"], row["merge_hash"][:10], e)
        else:
            out["tapd_id"] = ""
            out["story_key"] = ""
            out["confidence"] = ""
            try:
                out.update(score_unlinked(row, deliveries))
            except Exception as e:  # noqa: BLE001
                out["error"] = f"summary: {e}"
        return out

    def _write(row: dict) -> None:
        nonlocal written, errors
        if row.get("error"):
            errors.append(f"{row['repo']}:{row['merge_hash'][:10]}: {row['error']}")
        with open(OUT_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        written += 1
        if written % 25 == 0 or written == len(todo):
            elapsed = time.monotonic() - t0
            rate = written / max(elapsed, 1e-6)
            eta = (len(todo) - written) / max(rate, 1e-6)
            log.info("v2 重基进度 %d/%d, 速率 %.2f/s, ETA %.0fmin, 失败 %d",
                     written, len(todo), rate, eta / 60, len(errors))

    if concurrency == 1:
        for row in todo:
            try:
                _write(_score_one(row))
            except Exception as e:  # noqa: BLE001
                _write({"repo": row["repo"], "merge_hash": row["merge_hash"], "error": str(e),
                        "scored_at": _dt.datetime.now().isoformat(timespec="seconds")})
    else:
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as ex:
            futs = {ex.submit(_score_one, r): r for r in todo}
            for f in concurrent.futures.as_completed(futs):
                row = futs[f]
                try:
                    _write(f.result())
                except Exception as e:  # noqa: BLE001
                    _write({"repo": row["repo"], "merge_hash": row["merge_hash"], "error": str(e),
                            "scored_at": _dt.datetime.now().isoformat(timespec="seconds")})

    tok = judges.get_token_usage()
    summary = {
        "v1_total": len(v1_rows),
        "scored_now": written,
        "already_done": len(done),
        "errors": errors,
        "token_usage": tok,
        "base_url": base_url,
        "model": model,
    }
    (SNAP_V2 / "rebase_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("v2 重基完成: %s", json.dumps(summary, ensure_ascii=False))
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, encoding="utf-8")
    print(json.dumps(main(), ensure_ascii=False, indent=2))
