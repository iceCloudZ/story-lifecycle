#!/usr/bin/env python
"""kb.py — agentic-RAG 知识查询 CLI（让执行器 agent 按需查项目知识）。

两档检索：
  kb graph <node>        精确/确定性：遍历 product-context-graph（service/table/feign/mq 结构）
  kb bugs <type|file>    语义/关键词：bug-prone 文件、磁铁、cycle-time（类级，来自 result_axis_phase2）
  kb playbook <type>     过程知识：该 task_type 的 playbook（高频文件/命令/坑）

输出简洁、token-conscious（不灌全文）。claude code 用 bash 调。
详见 docs/2026-06-29-agentic-rag-kb-tool.md。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

# 强制 UTF-8 输出——agent 从 hc-all 调时控制台可能默认 GBK，不设会乱码。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_PROJ = Path(__file__).resolve().parents[1]
OUT = _PROJ / "scripts" / "out"
GRAPH = Path(os.environ.get("HC_STORY_KNOWLEDGE", "D:/hc-all/.story/knowledge"))
GRAPH_JSON = GRAPH / "graph" / "product-context-graph.json"
PLAYBOOKS = GRAPH / "playbooks"

TASK_TYPES = [
    "credit-limit", "fund-flow", "message-notify", "marketing", "user-profile",
    "order", "integration", "gateway-infra", "data-sql", "frontend", "deploy", "debug",
]


def _load(name):
    p = OUT / name
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_graph():
    if not GRAPH_JSON.exists():
        return None
    try:
        return json.loads(GRAPH_JSON.read_text(encoding="utf-8"))
    except Exception:
        return None


# ---- graph tier: 确定性图遍历 ----

_EDGE_HINT = {
    "HAS_DOMAIN": "domain", "CALLS_FEIGN": "calls feign", "READS_TABLE": "reads table",
    "WRITES_TABLE": "writes table", "PUBLISHES_MQ": "publishes mq", "CONSUMES_MQ": "consumes mq",
    "HAS_STATE_MACHINE": "state machine", "HAS_SCENARIO": "scenario", "HAS_STATE": "state",
}


def cmd_graph(query: str):
    g = _load_graph()
    if not g:
        print(f"(graph not found: {GRAPH_JSON})")
        return
    nodes = g.get("nodes", [])
    edges = g.get("edges", [])
    by_id = {n["id"]: n for n in nodes}
    q = query.lower().strip()
    matches = [n for n in nodes if q in (n.get("name", "") + " " + n.get("id", "")).lower()]
    if not matches:
        print(f"(no graph node matches '{query}'. graph is service/table/feign/mq 级，类名查 `kb bugs <file>`)")
        return
    for n in matches[:3]:
        nid = n["id"]
        print(f"== [{n.get('type')}] {n.get('name')} ({nid}) ==")
        extra = {k: v for k, v in n.items() if k not in ("id", "type", "name")}
        if extra:
            print("  " + ", ".join(f"{k}={v}" for k, v in extra.items()))
        grouped = defaultdict(list)
        for e in edges:
            if e["source"] == nid:
                tgt = by_id.get(e["target"], {})
                grouped[f"  → {_EDGE_HINT.get(e['type'], e['type'])}"].append(tgt.get("name", e["target"]))
            elif e["target"] == nid:
                src = by_id.get(e["source"], {})
                grouped[f"  ← {_EDGE_HINT.get(e['type'], e['type'])} (from)"].append(src.get("name", e["source"]))
        for label, names in sorted(grouped.items()):
            shown = names[:8]
            more = f" (+{len(names)-8})" if len(names) > 8 else ""
            print(f"{label}: {', '.join(shown[:8])}{more}")
        print("")


# ---- bugs tier: 关键词语义（bug-prone / 磁铁 / cycle-time）----

def cmd_bugs(query: str):
    p2 = _load("result_axis_phase2.json") or {}
    bg = _load("bug_story_graph.json") or {}
    tt = _load("story_task_types.json") or []
    q = query.strip()

    # 模式 A：task_type
    if q in TASK_TYPES:
        patterns = (p2.get("patterns_by_task_type") or {}).get(q, [])
        ct = (p2.get("cycle_time") or {}).get("by_task_type", {}).get(q, {})
        print(f"== {q}: bug-prone ==")
        if patterns:
            print("  high-risk files (bug_weight):")
            for p in patterns[:6]:
                print(f"    {p['file']}  (commits={p['commit_count']}, bug_weight={p['bug_weight']})")
        if ct and ct.get("n"):
            print(f"  cycle-time: median={ct['median']}h p90={ct['p90']}h mean={ct['mean']}h (n={ct['n']})")
        # magnets of this type
        type_map = {x["story_key"]: x.get("task_type") for x in tt if isinstance(x, dict)}
        mags = [(s["title"][:42], s["bug_count"]) for s in (bg.get("stories") or [])
                if type_map.get(s["story_key"]) == q and s.get("bug_count", 0) > 0]
        mags.sort(key=lambda x: -x[1])
        if mags:
            print("  bug magnets:")
            for t, n in mags[:4]:
                print(f"    {n} bugs | {t}")
        return

    # 模式 B：文件/类名
    q1 = q.lower()
    hits = []
    for tp, pats in (p2.get("patterns_by_task_type") or {}).items():
        for p in pats:
            if q1 in p["file"].lower():
                hits.append((tp, p))
    if hits:
        print(f"== bug-prone: '{query}' ==")
        for tp, p in hits[:8]:
            print(f"    [{tp}] {p['file']}  (commits={p['commit_count']}, bug_weight={p['bug_weight']})")
    else:
        print(f"(no bug-prone file matches '{query}'. 试 task_type 如 credit-limit/marketing，或更短文件名)")
        print(f"  known task_types: {', '.join(TASK_TYPES)}")


def cmd_playbook(task_type: str):
    if task_type not in TASK_TYPES:
        print(f"(unknown task_type '{task_type}'. known: {', '.join(TASK_TYPES)})")
        return
    p = PLAYBOOKS / f"{task_type}.md"
    if not p.exists():
        print(f"(no playbook for {task_type})")
        return
    text = p.read_text(encoding="utf-8")
    lines = text.splitlines()
    print(f"== playbook: {task_type} ({len(lines)} lines) ==\n")
    # head 关键段（高频文件 + 失败），不灌全文
    out, in_section = [], False
    for ln in lines:
        s = ln.strip()
        if s.startswith("## 高频") or s.startswith("## 常见失败") or s.startswith("## 常用操作"):
            in_section = True; out.append(ln); continue
        if s.startswith("## ") and in_section:
            in_section = False
        if in_section:
            out.append(ln)
        if len(out) > 40:
            break
    print("\n".join(out) if out else "\n".join(lines[:40]))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("graph", help="精确/确定性：遍历项目图（service/table/feign/mq）").add_argument("query")
    bp = sub.add_parser("bugs", help="语义/关键词：bug-prone 文件、磁铁、cycle-time")
    bp.add_argument("query", help="task_type(如 credit-limit) 或 文件/类名片段")
    pp = sub.add_parser("playbook", help="过程知识：该 task_type 的 playbook")
    pp.add_argument("task_type", choices=TASK_TYPES)
    args = ap.parse_args()
    if args.cmd == "graph":
        cmd_graph(args.query)
    elif args.cmd == "bugs":
        cmd_bugs(args.query)
    elif args.cmd == "playbook":
        cmd_playbook(args.task_type)


if __name__ == "__main__":
    main()
