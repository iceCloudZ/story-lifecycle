"""v2 重基补丁：linked 行补 delivery_score（与 v1 结构对齐）+ no-reference 修正。

- 210 行 linked（含 2 行无参照物）补 judge_delivery（Go）
- 2 行 no-reference：error → conformance_skipped（v1 行为：无参照物只评 delivery，不标失败）
追加写盘，load 时后写覆盖同键。
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger("eval.v2_backfill")

PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent
SNAP_V1 = PACKAGE_ROOT / "dataset" / "snapshot_20260805"
SNAP_V2 = PACKAGE_ROOT / "dataset" / "snapshot_v2_20260806"
OUT_PATH = SNAP_V2 / "merge_scores.jsonl"


def load_v2() -> dict[tuple[str, str], dict]:
    out: dict[tuple[str, str], dict] = {}
    for line in OUT_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            out[(rec["repo"], rec["merge_hash"])] = rec
    return out


def load_v1_deliveries() -> dict[tuple[str, str], dict]:
    out: dict[tuple[str, str], dict] = {}
    for line in (SNAP_V1 / "deliveries.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            out[(rec["repo"], rec["merge_hash"])] = rec
    return out


def main() -> dict:
    sys.path.insert(0, str(PACKAGE_ROOT.parent / "story-lifecycle" / "src"))
    sys.path.insert(0, str(PACKAGE_ROOT / "src"))

    from eval.judges import configure_llm_env

    configure_llm_env()
    print("base_url:", os.environ.get("STORY_LLM_BASE_URL"))

    rows = load_v2()
    deliveries = load_v1_deliveries()
    todo = [r for r in rows.values() if r.get("tapd_id") or r.get("story_key") and not r.get("delivery_score")]
    log.info("补 delivery_score: %d 行", len(todo))

    import concurrent.futures

    concurrency = int(os.environ.get("EVAL_LLM_CONCURRENCY", "4"))

    def _patch(r: dict) -> None:
        d = deliveries.get((r["repo"], r["merge_hash"]), {})
        commits = d.get("commits") or []
        try:
            from eval.judges import judge_delivery

            ds = judge_delivery(commits, r["repo"], r.get("branch", ""))
            r["delivery_score"] = ds.model_dump()
        except Exception as e:  # noqa: BLE001
            r["delivery_error"] = str(e)
        # no-reference 修正（不标 error，与 v1 行为一致）
        if r.get("error") == "no reference":
            r.pop("error", None)
            r["conformance_skipped"] = "no reference"
        r["scored_at"] = _dt.datetime.now().isoformat(timespec="seconds")
        with open(OUT_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    fixed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = {ex.submit(_patch, r): r for r in todo}
        for f in concurrent.futures.as_completed(futs):
            f.result()
            fixed += 1
            if fixed % 25 == 0 or fixed == len(todo):
                log.info("backfill %d/%d", fixed, len(todo))

    print(f"patched {fixed} rows (delivery backfill + no-reference)")
    return {"patched": fixed}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, encoding="utf-8")
    print(json.dumps(main(), ensure_ascii=False, indent=2))
