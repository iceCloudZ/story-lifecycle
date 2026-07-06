"""Clean reset for story 065458 before single-driver clean run.
- status=idle / current_stage=design / last_error=""
- clear execution state (_active_execution/_recovery_attempt/last_done_data/
  last_verify_summary/repair_packet_path/review_round_count_*)
- KEEP _agent_actions but drop the polluted repair action (restore clean
  [design, build, verify] launch plan)
Idempotent; prints before/after so we can verify.
"""
import sys, json
sys.path.insert(0, "packages/story-lifecycle/src")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from story_lifecycle.entry.cli.setup import load_config_to_env; load_config_to_env()
from story_lifecycle.infra.db import models as db

K = "tapd-1144381896001065458"
s = db.get_story(K)
ctx = json.loads(s.get("context_json") or "{}")

print("=== BEFORE ===")
print("status/stage:", s["status"], "/", s["current_stage"])
aa = ctx.get("_agent_actions", [])
print("_agent_actions:", [(a.get("stage"), a.get("action"), str(a.get("focus",""))[:30]) for a in aa])
for k in ["_active_execution", "_recovery_attempt", "last_done_data",
          "last_verify_summary", "repair_packet_path"]:
    print(f"  {k}:", "SET" if ctx.get(k) else "(empty)")
print("  review_round_count_*:", [k for k in ctx if k.startswith("review_round_count")])

# 1) clear execution state
for k in ["_active_execution", "_recovery_attempt", "last_done_data",
          "last_verify_summary", "repair_packet_path"]:
    ctx.pop(k, None)
for k in [k for k in list(ctx) if k.startswith("review_round_count")]:
    ctx.pop(k, None)

# 2) drop polluted repair action(s) — keep only the original launch plan.
#    A repair action's focus starts with "repair round"; drop those.
clean_aa = [a for a in aa if not str(a.get("focus", "")).lstrip().lower().startswith("repair round")]
ctx["_agent_actions"] = clean_aa

# 3) reset story status
db.update_story(K, status="idle", current_stage="design", last_error="",
                context_json=json.dumps(ctx, ensure_ascii=False))

print()
print("=== AFTER ===")
s2 = db.get_story(K)
ctx2 = json.loads(s2.get("context_json") or "{}")
print("status/stage:", s2["status"], "/", s2["current_stage"], "| err:", repr(s2.get("last_error","")))
print("_agent_actions:", [(a.get("stage"), a.get("action")) for a in ctx2.get("_agent_actions", [])])
for k in ["_active_execution", "_recovery_attempt", "last_done_data", "last_verify_summary"]:
    print(f"  {k}:", "SET(!)" if ctx2.get(k) else "(empty)")
print("  review_round_count_*:", [k for k in ctx2 if k.startswith("review_round_count")])
