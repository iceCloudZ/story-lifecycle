# -*- coding: utf-8 -*-
"""A 线结论：预注册规则（§3.3，一字不改）+ 逐条明细。"""
import json
from collections import Counter

rows = [json.loads(l) for l in open(r"D:\github\story-lifecycle\packages\eval\results\i4_abc_20260812.jsonl", encoding="utf-8") if l.strip()]
g1 = [r for r in rows if r["grid"] == "g1"]
g2 = [r for r in rows if r["grid"] == "g2"]
BLOCK = {"retry", "fail", "escalate"}

g1_pass = sum(1 for r in g1 if r.get("gate_decision") == "advance")
g1_rate = g1_pass / len(g1)
g2_block = sum(1 for r in g2 if r.get("gate_decision") in BLOCK)
g2_rate = g2_block / len(g2)

print(f"格 1 放行率: {g1_pass}/{len(g1)} = {g1_rate:.0%}（判定: {dict(Counter(r.get('gate_decision') for r in g1))}）")
print(f"格 2 拦截率: {g2_block}/{len(g2)} = {g2_rate:.0%}（判定: {dict(Counter(r.get('gate_decision') for r in g2))}）")
print()
print("预注册决策规则：")
print("- 格1>=70% 且 格2>=80% → 裁判健全：议题 2 关闭")
print("- 格1>=70% 且 格2<50%  → 证据驱动实锤：议题 2 升级 P1")
print("- 格1<50%             → 系统性保守：查 prompt 保守偏向")
print("- 中间地带（格2 拦 50-80%）→ 逐条归因")
print()
print("== 格 1 逐条（应放行） ==")
for r in sorted(g1, key=lambda x: x["merge10"]):
    print(f"  {r['merge10']} {r['repo']} v2=({r.get('v2_align')},{r.get('v2_cov')}) -> {r.get('gate_decision')}")
    print(f"     {(r.get('gate_reason_full') or '')[:200]}")
print()
print("== 格 2 逐条（应拦截） ==")
for r in sorted(g2, key=lambda x: x["merge10"]):
    mark = "漏拦!" if r.get("gate_decision") not in BLOCK else ""
    print(f"  {r['merge10']} {r['repo']} v2=({r.get('v2_align')},{r.get('v2_cov')}) -> {r.get('gate_decision')} {mark}")
