# -*- coding: utf-8 -*-
"""B 线代码管线（本地侧）：剥密 → 近一年 bundle → manifest ledger。

v1 手动形态：每次跑一个 batch（--repos 仓名列表），产物（bundle + manifest）
由操作者 scp 到 101 再跑 `eval.b_code_ingest`（101 侧）。跑稳后挂定时。

- 剥密：filter-repo --invert-paths --path-glob（规则按 gitleaks 报告定稿，--scrub 可多次）
- 历史范围：近一年（SINCE）。agent 需要 log/blame/worktree，保留历史、剥敏感文件。
- 增量：scrub clone 常驻 D:/hc-all-scrub/，bundle 区间 = refs/b101-marker..HEAD，
  首次为全量近一年；下次跑自动增量。
- 101 只收本管线产出：clone from bundle 无 origin，天然断内网。

用法:
  python -m eval.b_code_export --repos hc-order,hc-limit --scrub "app*.yml" --out D:/hc-export/20260813-1
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(r"D:/hc-all")           # 业务仓总目录（本地，有内网）
SCRUB_ROOT = Path(r"D:/hc-all-scrub")    # 剥密 clone 常驻目录（增量 marker 存放处）
SINCE = "1 year ago"


def run(cmd: list, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(cwd) if cwd else None,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repos", required=True, help="逗号分隔仓名（D:/hc-all 下）")
    ap.add_argument("--scrub", action="append", default=[],
                    help="filter-repo --invert-paths --path-glob 参数（可多次）")
    ap.add_argument("--replace-text", default=None,
                    help="filter-repo --replace-text 规则文件（regex==>replacement 行）")
    ap.add_argument("--out", required=True, help="产物目录（bundle + manifest）")
    args = ap.parse_args()

    # git filter-repo 由 pip 装进 venv Scripts——让 git 子命令找得到它
    os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    SCRUB_ROOT.mkdir(parents=True, exist_ok=True)
    batch_id = f"code-{datetime.now():%Y%m%d-%H%M%S}"
    items = []
    for name in [s.strip() for s in args.repos.split(",") if s.strip()]:
        src = REPO_ROOT / name
        if not (src / ".git").exists():
            print(f"[skip] {name}: 源仓缺失")
            items.append({"repo": name, "error": "missing source repo"})
            continue
        scrub = SCRUB_ROOT / name
        if not scrub.exists():
            # --no-local：本地 clone 默认走硬链接，filter-repo 会拒
            # ("expected freshly packed repo")——必须真拷贝。
            r = run(["git", "clone", "--no-local", str(src), str(scrub)])
            if r.returncode != 0:
                items.append({"repo": name, "error": f"clone fail: {r.stderr[-200:]}"})
                continue
        else:
            run(["git", "fetch", "--all"], cwd=scrub)

        # 剥密 + 1y 截断：只在 scrub clone 首次建仓时跑（sentinel 在外层，
        # 不污染工作树）。规则变更需删 SCRUB_ROOT/<name> + sentinel 重建。
        sentinel = SCRUB_ROOT / f"{name}.scrubbed"
        if not sentinel.exists():
            # 1y 截断：摘掉 1y 边界 commit 的父指针（graft）→ filter-repo 烙进 →
            # 历史变 ~1y 深且 bundle 自包含。2026-08-13 教训：`bundle create
            # --since=1y --all` 会把边界父记成 prerequisite，101 空仓 clone
            # 直接失败（10/13 仓中招）；graft 后无 prerequisite。
            oldest = run(["git", "rev-list", f"--since={SINCE}", "--all"],
                         cwd=scrub).stdout.strip().splitlines()
            grafted = False
            if oldest:
                run(["git", "replace", "--graft", oldest[-1]], cwd=scrub)
                grafted = True
            if args.scrub or args.replace_text or grafted:
                cmd = ["git", "filter-repo"]
                if args.replace_text:
                    # 规则文件规范化：utf-8-sig 去 BOM + 全量剥 \r。注意 Windows
                    # 上 text-mode 写文件会把 \n 翻回 \r\n（write_text 亦然），
                    # 必须显式 newline="\n"（2026-08-13 hc-third-party 实测教训）。
                    raw = Path(args.replace_text).read_text(encoding="utf-8-sig")
                    tmp = SCRUB_ROOT / ".replace-text-normalized.txt"
                    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                        f.write(raw.replace("\r", ""))
                    cmd += ["--replace-text", str(tmp)]
                if args.scrub:
                    cmd += ["--invert-paths"] + sum((["--path-glob", s] for s in args.scrub), [])
                r = run(cmd, cwd=scrub)
                if r.returncode != 0:
                    items.append({"repo": name, "error": f"filter-repo fail: {r.stderr[-300:]}"})
                    continue
            # 删全部 tag：防旧 tag 把 1y 前历史拖回 bundle（graft 只截断分支）
            tags = run(["git", "tag", "-l"], cwd=scrub).stdout.splitlines()
            if tags:
                run(["git", "tag", "-d"] + tags, cwd=scrub)
            sentinel.write_text("ok", encoding="utf-8")

        head = run(["git", "rev-parse", "HEAD"], cwd=scrub).stdout.strip()
        marker = run(["git", "rev-parse", "--verify", "refs/b101-marker"],
                     cwd=scrub).stdout.strip()
        bundle = out / f"{name}-1y.bundle"
        if marker:
            cmd = ["git", "bundle", "create", str(bundle), f"{marker}..{head}", "--all"]
        else:
            # 历史已被 graft 截断，bundle --all 即自包含（勿加 --since）
            cmd = ["git", "bundle", "create", str(bundle), "--all"]
        r = run(cmd, cwd=scrub)
        if r.returncode != 0:
            items.append({"repo": name, "error": f"bundle fail: {r.stderr[-300:]}"})
            continue
        run(["git", "update-ref", "refs/b101-marker", head], cwd=scrub)
        items.append({
            "repo": name, "bundle": bundle.name,
            "sha256": sha256(bundle), "size": bundle.stat().st_size,
            "head_sha": head, "marker": marker or "(first)",
            "scrub_paths": args.scrub, "history": SINCE,
        })

    manifest = {
        "batch_id": batch_id,
        "date": datetime.now().isoformat(timespec="seconds"),
        "cleaning_rules_version": "v1",
        "direction": "local->101",
        "items": items,
    }
    mp = out / f"manifest-{batch_id}.json"
    mp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[done] batch={batch_id} 产物目录={out}")
    for it in items:
        status = it.get("error") or "ok"
        print(f"  {it['repo']:16s} {status}")


if __name__ == "__main__":
    main()
