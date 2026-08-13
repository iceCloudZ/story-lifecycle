# -*- coding: utf-8 -*-
"""B 线最终统计（用户口径）：按 tapd_id 去重保留最新行；修复前旧数据单列不计入完成率。"""
import json
from collections import Counter
from pathlib import Path

rows = [json.loads(l) for l in open(r"D:\github\story-lifecycle\packages\eval\results\b_line_20260812.jsonl", encoding="utf-8") if l.strip()]
latest = {}
for r in rows:
    latest[r["tapd_id"]] = r

new_rows = [r for r in latest.values() if r.get("story_status") is not None]  # auto-advance 修复后（带终态）
old_rows = [r for r in latest.values() if r.get("story_status") is None]      # 修复前旧数据

print(f"总行数 {len(rows)}，去重后 {len(latest)}（新行 {len(new_rows)} / 修复前旧数据 {len(old_rows)}）")
print()
print("== 主口径（修复后最新行，计入完成率） ==")
print(dict(Counter(r.get("cls") for r in new_rows)))
n_ok = sum(1 for r in new_rows if r.get("cls") == "ok")
print(f"真完成率: {n_ok}/{len(new_rows)} = {n_ok/max(len(new_rows),1):.0%}")
print()
print("== 修复前旧数据（参考，不计入完成率） ==")
print(dict(Counter(r.get("cls") for r in old_rows)))
print()
print("== 明细（最新行） ==")
for r in new_rows:
    print(f"  {r['tapd_id']} {r.get('cls'):22s} {r.get('elapsed_s')}s story={r.get('story_status')}/{r.get('story_stage')}/{r.get('story_lifecycle')} done={r.get('done_files')}")
for r in old_rows:
    print(f"  [旧] {r['tapd_id']} {r.get('cls'):22s} {r.get('note', '')[:40]}")
