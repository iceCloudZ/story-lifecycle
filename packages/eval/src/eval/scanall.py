"""全量扫描评分 — 对 ``deliveries.jsonl`` 全部 merge 逐个评分（不只 core 集）。

评什么（按关联状态分档）:

- 有关联 story（high/official/已确认）: ``ConformanceScore``（参照物
  spec > PRD > TAPD 描述）+ ``DeliveryScore``
- 无关联 story: ``MergeSummary`` + ``DeliveryScore``;摘要随行落盘,
  作为第二轮模糊关联的素材（新命中的进待确认列表）

断点续跑: 结果按 ``(repo, merge_hash)`` 追加写 ``results/merge_scores.jsonl``
（每完成一个追加一行）,重跑自动跳过已完成;支持 ``--limit N`` 分批。

diff 预算: 单 merge 送审内容设上限（diffstat + 按 churn 排序的 top 文件 diff,
总量截断 ~80k token）;超限的结果标 ``truncated: true``。

进度: 每 50 个输出一次（已完成/剩余/速率/ETA）;连续失败率突增（服务端抖动）
时短暂退避后自动恢复。启动前检查 ``scan_all.lock``（同类进程在跑则中止）。
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import subprocess
import time
from pathlib import Path
from statistics import mean
from typing import Any

from . import dataset
from . import gitindex
from . import judges

log = logging.getLogger("eval.scanall")

PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent
RESULTS_DIR = PACKAGE_ROOT / "results"
DATASET_DIR = PACKAGE_ROOT / "dataset"
SCORES_PATH = RESULTS_DIR / "merge_scores.jsonl"
LOCK_PATH = RESULTS_DIR / "scan_all.lock"

DIFF_BUDGET_CHARS = 300_000  # 收集上限（judge 内部再截 120k）
MAX_FILES = 40  # 按 churn 排序参与送审的文件上限
PROGRESS_EVERY = 50
BURST_FAILURES = 5
BURST_BACKOFF_S = 120


def _git(repo: Path, args: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "--no-pager", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _repo_path(repo_name: str) -> Path | None:
    if repo_name == "hc-admin":
        p = Path("D:/hc-all/frontends/hc-admin")
    else:
        p = Path("D:/hc-all") / repo_name
    return p if p.is_dir() else None


# ---------------------------------------------------------------------------
# 关联索引
# ---------------------------------------------------------------------------


def load_match_index() -> tuple[dict[tuple[str, str], dict], dict[str, dict]]:
    """(repo, merge_hash) → {entity, confidence};tapd_id → tapd story。"""
    idx: dict[tuple[str, str], dict] = {}
    tapd: dict[str, dict] = {}
    p = DATASET_DIR / "stories_matched.jsonl"
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
                    idx[key] = {"entity": ent, "confidence": conf}
    tp = DATASET_DIR / "tapd_stories.jsonl"
    if tp.exists():
        for line in tp.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                tapd[rec["tapd_id"]] = rec
    return idx, tapd


def _tapd_desc(tapd: dict[str, dict], tid: str) -> str:
    import re

    desc = (tapd.get(tid) or {}).get("description") or ""
    desc = re.sub(r"<[^>]+>", " ", desc)
    desc = re.sub(r"\s+", " ", desc)
    return desc.strip()[:40_000]


def _evidence_reference(entity: dict) -> tuple[str, str]:
    """证据目录里取参照物:spec > PRD。返回 (text, type)。"""
    ed = entity.get("evidence_dir") or ""
    if not ed:
        return "", ""
    d = Path(ed)
    if not d.is_dir():
        return "", ""
    for doc_key, cands in (("spec", ["spec.md", "Spec.md", "design.md"]), ("prd", ["PRD.md", "prd.md", "Prd.md"])):
        for cand in cands:
            f = d / cand
            if f.exists() and f.stat().st_size > 0:
                return dataset._read_text_robust(f), doc_key
    return "", ""


# ---------------------------------------------------------------------------
# diff 预算
# ---------------------------------------------------------------------------


def _diff_text(repo_name: str, merge_hash: str) -> tuple[str, bool]:
    """按 churn 排序取 top 文件 diff,diffstat 前缀,总量截断。返回 (text, truncated)。"""
    repo = _repo_path(repo_name)
    if repo is None:
        return "", True
    base = f"{merge_hash}^1"
    # 每文件 churn:numstat
    r = _git(repo, ["diff", "--numstat", "-M", base, merge_hash], timeout=120)
    if r.returncode != 0:
        return "", True
    churn: list[tuple[int, str]] = []
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        ins, dele = parts[0], parts[1]
        path = parts[2]
        if ins == "-" or dele == "-":
            continue
        try:
            churn.append((int(ins) + int(dele), path))
        except ValueError:
            continue
    churn.sort(reverse=True)

    stat = ""
    r2 = _git(repo, ["diff", "--shortstat", base, merge_hash], timeout=120)
    if r2.returncode == 0 and r2.stdout.strip():
        stat = r2.stdout.strip()

    text = stat + "\n"
    used = len(text)
    truncated = len(churn) > MAX_FILES
    for _, path in churn[:MAX_FILES]:
        r3 = _git(repo, ["diff", "-U3", base, merge_hash, "--", path], timeout=180)
        if r3.returncode != 0:
            continue
        chunk = r3.stdout
        if used + len(chunk) > DIFF_BUDGET_CHARS:
            truncated = True
            break
        text += chunk
        used += len(chunk)
    return text, truncated


# ---------------------------------------------------------------------------
# 单 merge 评分
# ---------------------------------------------------------------------------


def score_delivery(delivery: dict, linked: dict | None, tapd: dict[str, dict]) -> dict[str, Any]:
    """对单个 merge 打分。linked 为 load_match_index 的 {entity, confidence} 或 None。"""
    row: dict[str, Any] = {
        "repo": delivery["repo"],
        "merge_hash": delivery["merge_hash"],
        "branch": delivery.get("branch", ""),
        "merged_at": delivery.get("merged_at", ""),
        "author": delivery.get("author", ""),
        "diffstat": delivery.get("diffstat", {}),
        "truncated": False,
        "scored_at": _dt.datetime.now().isoformat(timespec="seconds"),
    }
    commits = delivery.get("commits", [])
    if linked:
        ent = linked["entity"]
        row["tapd_id"] = ent.get("tapd_id") or ""
        row["story_key"] = ent.get("story_key") or ""
        row["confidence"] = linked["confidence"]
        ref, ref_type = _evidence_reference(ent)
        if not ref:
            ref = _tapd_desc(tapd, ent.get("tapd_id") or "")
            ref_type = "tapd" if ref else ""
        if ref:
            diff_text, truncated = _diff_text(delivery["repo"], delivery["merge_hash"])
            row["truncated"] = truncated
            try:
                cscore = judges.judge_conformance(ref, ref_type, diff_text)
                row["conformance_score"] = cscore.model_dump()
            except Exception as e:  # noqa: BLE001
                row["error"] = f"conformance: {e}"
                log.warning("Conformance 失败 %s:%s: %s", delivery["repo"], delivery["merge_hash"][:10], e)
        else:
            log.info("%s:%s 无参照物,只评 delivery", delivery["repo"], delivery["merge_hash"][:10])
    else:
        row["tapd_id"] = ""
        row["story_key"] = ""
        row["confidence"] = ""
        try:
            ms = judges.judge_merge_summary(commits, delivery["repo"], delivery.get("branch", ""), delivery.get("diffstat", {}))
            row["merge_summary"] = ms.model_dump()
        except Exception as e:  # noqa: BLE001
            row["error"] = f"summary: {e}"
            log.warning("MergeSummary 失败 %s:%s: %s", delivery["repo"], delivery["merge_hash"][:10], e)

    try:
        dscore = judges.judge_delivery(commits, delivery["repo"], delivery.get("branch", ""))
        row["delivery_score"] = dscore.model_dump()
    except Exception as e:  # noqa: BLE001
        row["error"] = (row.get("error") + "; " if row.get("error") else "") + f"delivery: {e}"
        log.warning("DeliveryScore 失败 %s:%s: %s", delivery["repo"], delivery["merge_hash"][:10], e)
    return row


# ---------------------------------------------------------------------------
# 断点续跑锁
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    """Windows 用 OpenProcess(SYNCHRONIZE) 检查进程存活。"""
    import ctypes

    PROCESS_SYNCHRONIZE = 0x00100000
    h = ctypes.windll.kernel32.OpenProcess(PROCESS_SYNCHRONIZE, 0, pid)
    if not h:
        return False
    ctypes.windll.kernel32.CloseHandle(h)
    return True


def acquire_lock() -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    if LOCK_PATH.exists():
        try:
            rec = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
            pid = int(rec.get("pid", 0))
            if pid and _pid_alive(pid):
                raise RuntimeError(
                    f"scan-all 已在运行(pid={pid},{rec.get('started','')})——请勿重复启动"
                )
        except (json.JSONDecodeError, OSError):
            pass
        log.warning("发现过期 lock,覆盖")
    LOCK_PATH.write_text(
        json.dumps({"pid": __import__("os").getpid(), "started": _dt.datetime.now().isoformat(timespec="seconds")}),
        encoding="utf-8",
    )


def release_lock() -> None:
    try:
        LOCK_PATH.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def run_scan_all(limit: int | None = None, results_dir: str | Path | None = None, max_attempts: int = 3) -> dict[str, Any]:
    res_dir = Path(results_dir) if results_dir else RESULTS_DIR
    res_dir.mkdir(parents=True, exist_ok=True)
    acquire_lock()
    try:
        return _run(res_dir, limit, max_attempts)
    finally:
        release_lock()


def _load_rows(path: Path) -> dict[tuple[str, str], dict]:
    """读 merge_scores.jsonl,(repo, merge_hash) 去重、同键后写覆盖。"""
    rows: dict[tuple[str, str], dict] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                rows[(rec.get("repo", ""), rec.get("merge_hash", ""))] = rec
    return rows


def _run(res_dir: Path, limit: int | None, max_attempts: int) -> dict[str, Any]:
    deliveries = gitindex.load_deliveries()
    if not deliveries:
        raise RuntimeError("deliveries.jsonl 为空——先跑 `eval index`")
    # 按 (repo, merge_hash) 去重（保留最后一条）
    seen: dict[tuple[str, str], dict] = {}
    for d in deliveries:
        seen[(d["repo"], d["merge_hash"])] = d
    ordered = list(seen.values())
    if limit:
        ordered = ordered[:limit]

    rows = _load_rows(SCORES_PATH)
    # 有 error 的行视为未完成——断点续跑时重试,不硬跳过
    done: set[tuple[str, str]] = {
        key for key, rec in rows.items() if not rec.get("error")
    }

    idx, tapd = load_match_index()
    todo = [(repo, h) for repo, h in [(d["repo"], d["merge_hash"]) for d in ordered] if (repo, h) not in done]
    log.info(
        "scan-all: 共 %d 个 merge,已完成 %d,待评 %d",
        len(ordered), len(done), len(todo),
    )

    errors: list[str] = []
    failures_in_row = 0
    t0 = time.monotonic()
    written = 0
    with open(SCORES_PATH, "a", encoding="utf-8") as f:
        for i, (repo, h) in enumerate(todo, 1):
            d = seen[(repo, h)]
            key = f"{repo}:{h[:10]}"
            linked = idx.get((repo, h))
            try:
                row = score_delivery(d, linked, tapd)
            except Exception as e:  # noqa: BLE001
                row = {
                    "repo": repo, "merge_hash": h, "branch": d.get("branch", ""),
                    "merged_at": d.get("merged_at", ""), "author": d.get("author", ""),
                    "error": str(e), "scored_at": _dt.datetime.now().isoformat(timespec="seconds"),
                }
                errors.append(f"{key}: {e}")
                log.exception("打分失败 %s", key)
                failures_in_row += 1
                if failures_in_row >= BURST_FAILURES:
                    log.warning("连续 %d 次失败（疑似服务端抖动）,退避 %ds 后恢复", failures_in_row, BURST_BACKOFF_S)
                    time.sleep(BURST_BACKOFF_S)
                    failures_in_row = 0
            else:
                if row.get("error"):
                    errors.append(f"{key}: {row['error']}")
                failures_in_row = 0
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            written += 1

            if written % PROGRESS_EVERY == 0 or written == len(todo):
                elapsed = time.monotonic() - t0
                rate = written / max(elapsed, 1e-6)
                eta = (len(todo) - written) / max(rate, 1e-6)
                log.info(
                    "scan-all 进度 %d/%d,速率 %.2f/s,ETA %.0fmin,失败 %d",
                    written, len(todo), rate, eta / 60, len(errors),
                )

    report = _render_report(res_dir, ordered, tapd, idx)
    date = _dt.date.today().strftime("%Y%m%d")
    md_path = res_dir / f"full_scan_{date}.md"
    md_path.write_text(report, encoding="utf-8")
    log.info("全量扫描报告: %s", md_path)
    return {
        "total": len(ordered),
        "scored_now": written,
        "already_done": len(done),
        "errors": errors,
        "report": str(md_path),
    }


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


def _dims(row: dict) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    conf = row.get("conformance_score") or {}
    for d in ("alignment", "coverage", "scope_drift"):
        if d in conf:
            out.append((f"conf.{d}", conf[d]))
    dv = row.get("delivery_score") or {}
    for d in ("message_quality", "granularity", "rework"):
        if d in dv:
            out.append((f"delivery.{d}", dv[d]))
    return out


def _render_report(res_dir: Path, ordered: list[dict], tapd: dict[str, dict], idx: dict) -> str:
    date = _dt.date.today().strftime("%Y%m%d")
    rows = list(_load_rows(res_dir / "merge_scores.jsonl").values())

    n_linked = sum(1 for r in rows if r.get("tapd_id"))
    n_unlinked = sum(1 for r in rows if not r.get("tapd_id") and not r.get("error"))
    n_err = sum(1 for r in rows if r.get("error"))
    n_trunc = sum(1 for r in rows if r.get("truncated"))
    by_dim: dict[str, list[int]] = {}
    for r in rows:
        for dim, score in _dims(r):
            by_dim.setdefault(dim, []).append(score)
    hist: dict[int, int] = {}
    for vals in by_dim.values():
        for v in vals:
            hist[v] = hist.get(v, 0) + 1

    lines = [
        f"# 全量扫描 {date}",
        "",
        f"- 扫描行数: {len(rows)}（deliveries 共 {len(ordered)}）",
        f"- 有关联 story: {n_linked} / 无关联(仅摘要): {n_unlinked} / 失败: {n_err}",
        f"- truncated: {n_trunc}（占比 {n_trunc / max(len(rows), 1):.1%}）",
        "",
        "## 总体分布",
        "",
        "| 维度 | 均分 | 中位 | 最小 | 最大 |",
        "|------|------|------|------|------|",
    ]
    for dim, vals in sorted(by_dim.items()):
        lines.append(f"| {dim} | {mean(vals):.2f} | {sorted(vals)[len(vals)//2]} | {min(vals)} | {max(vals)} |")
    lines += ["", "| 分数 | 出现次数 |", "|------|----------|"]
    for s in sorted(hist):
        lines.append(f"| {s} | {hist[s]} |")

    lines += ["", "## 低分 drift case Top 20", ""]
    drift = sorted(
        (r for r in rows if (r.get("conformance_score") or {}).get("alignment") is not None),
        key=lambda r: r["conformance_score"]["alignment"],
    )
    if drift:
        for r in drift[:20]:
            cf = r["conformance_score"]
            lines.append(
                f"- **{r['repo']}:{r['merge_hash'][:10]}** alignment={cf['alignment']} "
                f"coverage={cf['coverage']} scope_drift={cf['scope_drift']} — {cf.get('summary','')[:80]}"
            )
    else:
        lines.append("（无 conformance 评分）")

    lines += ["", "## 按 repo", ""]
    by_repo: dict[str, list[dict]] = {}
    for r in rows:
        by_repo.setdefault(r["repo"], []).append(r)
    for repo, rs in sorted(by_repo.items(), key=lambda kv: -len(kv[1])):
        confs = [r["conformance_score"] for r in rs if r.get("conformance_score")]
        if confs:
            avg = mean(c["alignment"] for c in confs)
            lines.append(f"- {repo}: {len(rs)} 个,conf.alignment 均分 {avg:.2f}")
        else:
            lines.append(f"- {repo}: {len(rs)} 个（无 conformance）")

    lines += ["", "## 按 author", ""]
    by_author: dict[str, list[dict]] = {}
    for r in rows:
        by_author.setdefault(r.get("author") or "?", []).append(r)
    for author, rs in sorted(by_author.items(), key=lambda kv: -len(kv[1]))[:15]:
        confs = [r["conformance_score"] for r in rs if r.get("conformance_score")]
        avg = mean(c["alignment"] for c in confs) if confs else float("nan")
        lines.append(f"- {author}: {len(rs)} 个,conf.alignment 均分 {avg:.2f}")

    lines += ["", "## 管线内 vs 管线外", ""]
    in_pipe = [r for r in rows if r.get("story_key")]
    out_pipe = [r for r in rows if not r.get("story_key")]
    def _avg_alignment(rs: list[dict]) -> str:
        confs = [r["conformance_score"] for r in rs if r.get("conformance_score")]
        return f"{mean(c['alignment'] for c in confs):.2f}" if confs else "-"
    lines.append(f"- 管线内（有 story_key）: {len(in_pipe)} 个,alignment {_avg_alignment(in_pipe)}")
    lines.append(f"- 管线外: {len(out_pipe)} 个,alignment {_avg_alignment(out_pipe)}")

    errs = [r for r in rows if r.get("error")]
    if errs:
        lines += ["", "## 失败列表", ""]
        for r in errs[:50]:
            lines.append(f"- {r['repo']}:{r['merge_hash'][:10]}: {r['error'][:120]}")
    return "\n".join(lines) + "\n"
