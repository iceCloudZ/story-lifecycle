# -*- coding: utf-8 -*-
"""B 线代码管线（101 侧）：校验 manifest sha256 → clone bundle → ledger 回执。

- 落位：~/story-eval/repos/<repo>（path B replay_set.json 指向的原址，替换旧镜像；
  旧镜像改名 <repo>.pre-scrub.bak 保留，可回退）
- clone from bundle：无 origin，天然断内网（纪律「101 只收管线产出，绝不自己拉」）
- 回执：results/b_code_receipts.jsonl（本地侧可 scp 拉取对账）

用法:
  python -m eval.b_code_ingest --manifest /path/manifest-code-xxx.json --bundles-dir /path/bundles
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

REPOS = Path.home() / "story-eval" / "repos"
RECEIPTS = Path.home() / "story-lifecycle" / "packages" / "eval" / "results" / "b_code_receipts.jsonl"


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--bundles-dir", required=True)
    args = ap.parse_args()

    m = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    bdir = Path(args.bundles_dir)
    REPOS.mkdir(parents=True, exist_ok=True)
    receipts = []
    for it in m["items"]:
        rec = {"batch_id": m["batch_id"], "repo": it.get("repo"), "result": ""}
        if it.get("error"):
            rec["result"] = "skipped"
            rec["error"] = it["error"]
            receipts.append(rec)
            continue
        b = bdir / it["bundle"]
        if not b.exists() or sha256(b) != it["sha256"]:
            rec["result"] = "sha_mismatch"
            receipts.append(rec)
            continue
        target = REPOS / it["repo"]
        if (target / ".git").exists():
            # 旧镜像换新：改名保留可回退，不直接 rm
            backup = REPOS / f"{it['repo']}.pre-scrub.bak"
            if backup.exists():
                subprocess.run(["rm", "-rf", str(backup)])
            subprocess.run(["mv", str(target), str(backup)])
        r = subprocess.run(["git", "clone", str(b), str(target)],
                           capture_output=True, text=True)
        rec["result"] = "ok" if r.returncode == 0 else f"clone_fail: {r.stderr[-200:]}"
        receipts.append(rec)

    with open(RECEIPTS, "a", encoding="utf-8") as f:
        for rec in receipts:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(json.dumps(receipts, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
