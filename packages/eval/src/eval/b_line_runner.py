# -*- coding: utf-8 -*-
"""B 线主 runner：50 条全链路回放（断点续跑 + 15min 看门狗 + infra 暂停 + nightly）。

- 每条 story 独立子进程（独立 STORY_HOME/workspace），15min 看门狗强杀
- 逐条落盘 results/b_line_20260812.jsonl
- infra 失败分类：PRD 缺失=skip / create 失败=infra_error / 看门狗超时=watchdog_timeout
- 连续 3 条 infra_error → 暂停退出报告（不硬闯）
- --nightly N：从候选池顺序取 N 条（EVAL_NIGHTLY_MAX 硬上限，默认 10）

用法:
  python -m eval.b_line_runner                     # 全量 50（跳已完成）
  python -m eval.b_line_runner --nightly 5         # 每晚 N 条
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# 路径可移植（101 接管）：默认从本文件位置推导，可用环境变量覆盖
PACKAGE_ROOT = Path(os.environ.get("EVAL_PACKAGE_ROOT", str(Path(__file__).resolve().parent.parent.parent)))
BASE = PACKAGE_ROOT / "dataset"
RESULTS = PACKAGE_ROOT / "results"
BATCH = BASE / "b_batch50_20260812.json"
OUT_JSONL = RESULTS / "b_line_20260812.jsonl"
PY = os.environ.get("EVAL_PYTHON", sys.executable)
ONE = os.environ.get("EVAL_B_LINE_ONE", str(Path(__file__).resolve().parent / "b_line_one.py"))
WATCHDOG_S = 3600  # 单条看门狗 60min（数据驱动：最近 15 条 6 条 900s 被强杀、真干活样本普遍 600-900s+；
                   #  900s 对全流程（design→verify→judge）偏紧——提到长尾上限，保留护栏防真卡死）
INFRA_PAUSE = 3  # 连续 3 条 infra 失败暂停


def classify(r: dict) -> str:
    status = r.get("status", "")
    if status == "skip":
        return "skip"
    if status == "failed":
        err = r.get("error", "")
        if "PRD 缺失" in err or "create:" in err:
            return "infra_error"
        return "failed"
    if status in ("leak",):
        return "infra_error"
    if status == "watchdog_timeout":
        return "watchdog_timeout"
    if status == "no_output":
        return "infra_error"
    # run_ok 但终态判定（B 线真完成 = story completed/failed）
    ss = r.get("story_status", "")
    if ss in ("completed", "failed"):
        return "ok" if status != "failed" else "failed"
    done = r.get("done_files") or []
    if ss == "paused" and done:
        return "confirm_gate_stall"
    if ss == "paused":
        return "no_artifacts_stall"
    if ss == "active":
        return "active_stall"
    return status or "ok"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--nightly", type=int, default=None, help="每晚从候选池顺序取 N 条")
    ap.add_argument("--only", default=None, help="只跑单个 tapd_id（调试）")
    args = ap.parse_args()

    batch = json.loads(BATCH.read_text(encoding="utf-8"))["batch"]
    max_n = min(args.nightly, int(os.environ.get("EVAL_NIGHTLY_MAX", "10"))) if args.nightly else len(batch)
    if args.only:
        batch = [b for b in batch if b["tapd_id"] == args.only]
    todo = batch[:max_n]
    print(f"[B] batch={len(batch)} todo={len(todo)} (nightly={args.nightly} max={max_n})", flush=True)

    done = {}
    if OUT_JSONL.exists():
        for l in OUT_JSONL.read_text(encoding="utf-8").splitlines():
            if l.strip():
                r = json.loads(l)
                # 各类 stall / infra 失败 = 未完成——不跳过，修复后重跑
                if r.get("cls") not in ("confirm_gate_stall", "no_artifacts_stall",
                                        "active_stall", "watchdog_timeout", "infra_error"):
                    done[r["tapd_id"]] = r
    pending = [b for b in todo if b["tapd_id"] not in done]
    print(f"[B] 断点: 已完成 {len(done)}，剩余 {len(pending)}", flush=True)

    infra_streak = 0
    f = open(OUT_JSONL, "a", encoding="utf-8")
    for i, s in enumerate(pending):
        tapd = s["tapd_id"]
        t0 = time.monotonic()
        try:
            proc = subprocess.Popen(
                [PY, "-X", "utf8", "-u", ONE, "--tapd", tapd],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding="utf-8", errors="replace",
                cwd=str(PACKAGE_ROOT),
            )
            try:
                out, err = proc.communicate(timeout=WATCHDOG_S)
                if proc.returncode == 0 and out.strip():
                    rec = json.loads(out.strip().splitlines()[-1])
                else:
                    rec = {"tapd_id": tapd, "status": "no_output",
                           "error": (err or out or "")[-300:]}
            except subprocess.TimeoutExpired:
                # Windows 进程树强杀：Popen/communicate 只杀直接子进程，
                # agent(opencode) 是孙进程——taskkill /T 杀整棵树，否则
                # 孙进程持有的管道会让 communicate 无限等待（实测 9608s 卡死）。
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        capture_output=True, timeout=15,
                    )
                except Exception:  # noqa: BLE001
                    pass
                try:
                    proc.communicate(timeout=30)
                except Exception:  # noqa: BLE001
                    pass
                rec = {"tapd_id": tapd, "status": "watchdog_timeout",
                       "error": f"> {WATCHDOG_S}s 看门狗强杀(进程树)"}
        except Exception as exc:  # noqa: BLE001 — 子进程启动异常
            rec = {"tapd_id": tapd, "status": "infra_error", "error": f"{exc.__class__.__name__}: {exc}"}
        rec["tapd_id"] = tapd
        rec["elapsed_s"] = round(time.monotonic() - t0, 1)
        rec["cls"] = classify(rec)
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()
        print(f"[{i+1}/{len(pending)}] {tapd} -> {rec['cls']} ({rec.get('elapsed_s')}s) {rec.get('error', '')[:80]}", flush=True)
        if rec["cls"] in ("infra_error", "watchdog_timeout"):
            infra_streak += 1
        else:
            infra_streak = 0
        if infra_streak >= INFRA_PAUSE:
            print(f"[B] 连续 {INFRA_PAUSE} 条 infra 失败 → 暂停（不硬闯）。已落盘 {len(done) + i + 1} 条", flush=True)
            break
    f.close()
    print("[B] DONE", flush=True)


if __name__ == "__main__":
    main()
