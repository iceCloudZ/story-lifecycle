"""Driver for story 1065570 (realtest, CAS-version start_story_async).
plan(if needed) -> start_story_async -> stay alive until story stops.
INFO logging -> stdout (utf-8). Executor runs in its thread."""
import sys, time, json, logging
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    stream=sys.stdout, encoding="utf-8")
sys.path.insert(0, "packages/story-lifecycle/src")
from story_lifecycle.entry.cli.setup import load_config_to_env; load_config_to_env()
from story_lifecycle.infra.db import models as db
from story_lifecycle.orchestrator.engine import planner
from story_lifecycle.orchestrator.engine.graph import start_story_async, is_story_running

KEY = "tapd-1144381896001065570"
db.update_story(KEY, status="idle", current_stage="design", last_error="")
s = db.get_story(KEY)
ctx = json.loads(s.get("context_json") or "{}")
if not ctx.get("_agent_actions"):
    planner.run_orchestrator_agent(KEY)
start_story_async(KEY)
for _ in range(160):  # ~80 min safety cap (kimi slower on new plan)
    time.sleep(30)
    try:
        if not is_story_running(KEY):
            time.sleep(5)
            if not is_story_running(KEY):
                break
    except Exception:
        pass
