"""Setup story 1065570 (联系人姓名校验) for a clean CAS-driver realtest run.
- reset: status=idle/design, profile=realtest, intake=ready, clear exec state
- set ctx.prd_path -> real PRD (so planner tells agent to read it)
- _agent_actions left empty -> deepseek run_orchestrator_agent plans fresh
- bind story_project: hc-config (project_id=1) on branch story-realtest-1065570 (from master)
Idempotent.
"""
import sys, json, os, sqlite3
sys.path.insert(0, "packages/story-lifecycle/src")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from story_lifecycle.entry.cli.setup import load_config_to_env; load_config_to_env()
from story_lifecycle.infra.db import models as db

K = "tapd-1144381896001065570"
PRD = r"D:/hc-all/story/1065570-联系人姓名校验/PRD.md"
assert os.path.isfile(PRD), f"PRD missing: {PRD}"

# 1) reset story state
s = db.get_story(K)
ctx = json.loads(s.get("context_json") or "{}")
ctx["prd_path"] = PRD
for k in ["_active_execution", "_recovery_attempt", "last_done_data",
          "last_verify_summary", "repair_packet_path", "_plan_confirmed"]:
    ctx.pop(k, None)
for k in [k for k in list(ctx) if k.startswith("review_round_count")]:
    ctx.pop(k, None)
ctx.pop("_agent_actions", None)  # fresh: let deepseek re-plan from PRD

db.update_story(
    K,
    status="idle",
    current_stage="design",
    last_error="",
    profile="realtest",
    intake_state="ready",
    context_json=json.dumps(ctx, ensure_ascii=False),
)
# clear any stale driver_claim from prior attempts (CAS token)
db.update_story(K, driver_claim=None)

# 2) bind story_project: hc-config (project_id=1) on story-realtest-1065570 (base master)
db_path = os.path.expanduser("~/.story-lifecycle/story.db")
con = sqlite3.connect(db_path)
con.execute("DELETE FROM story_project WHERE story_key=?", (K,))
con.execute(
    "INSERT INTO story_project (story_key, project_id, branch, base_branch, "
    "worktree_state, source, created_at, updated_at) "
    "VALUES (?, 1, ?, 'master', 'unprepared', 'agent', "
    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
    (K, "story-realtest-1065570"),
)
con.commit()
print("bound:", list(con.execute(
    "select story_key,project_id,branch,base_branch from story_project where story_key=?", (K,))))
con.close()

# 3) verify
s2 = db.get_story(K)
ctx2 = json.loads(s2.get("context_json") or "{}")
print("story:", s2["status"], "/", s2["current_stage"],
      "profile=", s2["profile"], "intake=", s2.get("intake_state"),
      "driver_claim=", s2.get("driver_claim"))
print("prd_path=", ctx2.get("prd_path"))
print("_agent_actions=", len(ctx2.get("_agent_actions", [])), "(0 -> deepseek will plan)")
