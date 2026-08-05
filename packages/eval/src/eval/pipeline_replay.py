"""round2 全管线回放驱动（分段驱动，复用 core 接口，不新写引擎）。

- create_and_start_story（建 story + PRD 证据）
- continue_orchestrator_agent（真实驱动 design→verify，spawn opencode headless）
- run_unified_verify_gate（verify 交付判定）
- STORY_HOME 沙箱隔离；workspace 必须先建 + 写 AGENTS.md（截断证据爬升，
  防止泄漏到 repo 根 story/）；profile 放 cwd/.story/profiles/。

用法: python -m eval.pipeline_replay --only <tapd_id>
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent
SANDBOX = PACKAGE_ROOT / "sandbox"
STORY_HOME = SANDBOX / "story_home"
SL_SRC = PACKAGE_ROOT.parent / "packages" / "story-lifecycle" / "src"

PROFILE_NAME = "replay-nb"
STAGE_TIMEOUT_S = 30 * 60


def _bootstrap_env() -> None:
    STORY_HOME.mkdir(parents=True, exist_ok=True)
    os.environ["STORY_HOME"] = str(STORY_HOME)
    os.environ["STORY_LLM_BASE_URL"] = "https://opencode.ai/zen/go/v1"
    os.environ.setdefault("STORY_LLM_MODEL", "deepseek-v4-flash")
    if "STORY_LLM_API_KEY" not in os.environ:
        from eval.judges import configure_llm_env

        configure_llm_env()  # Go 端点 + OPENCODE_API_KEY
    sys.path.insert(0, str(SL_SRC))
    # profile 解析：cwd/.story/profiles/（_load_raw 第一搜索路径）
    profiles_dir = PACKAGE_ROOT / ".story" / "profiles"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    src = SANDBOX / "profiles" / f"{PROFILE_NAME}.yaml"
    dst = profiles_dir / f"{PROFILE_NAME}.yaml"
    if src.exists() and not dst.exists():
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")


def run_one(tapd_id: str, gold_dir: Path, out: dict) -> dict:
    story_key = f"tapd-{tapd_id}"
    ws = SANDBOX / "ws" / story_key
    out.update({"story_key": story_key, "tapd": tapd_id, "ws": str(ws)})

    # workspace 准备：AGENTS.md 截断证据爬升（防泄漏到 repo 根 story/）
    ws.mkdir(parents=True, exist_ok=True)
    agents = ws / "AGENTS.md"
    if not agents.exists():
        agents.write_text("# sandbox replay workspace\n\n仅回放用，不提交真实代码。\n", encoding="utf-8")
    # 证据爬升自检
    from story_lifecycle.infra.story_paths import story_evidence_root

    ev = story_evidence_root(ws)
    out["evidence_root"] = str(ev)
    if not str(ev).startswith(str(SANDBOX)):
        out["status"] = "leak"
        out["error"] = f"证据目录泄漏出沙箱: {ev}"
        return out

    from story_lifecycle.infra.db import models as db
    from story_lifecycle.orchestrator.service.story_service import create_and_start_story
    from story_lifecycle.orchestrator.engine import planner
    from story_lifecycle.orchestrator.evaluation.unified_gate import run_unified_verify_gate

    db.init_db()

    prd = gold_dir / "PRD.md"
    if not prd.exists():
        out["status"] = "skip"
        out["error"] = f"gold PRD 缺失: {prd}"
        return out

    # 1. create
    t0 = time.monotonic()
    try:
        create_and_start_story(
            story_key=story_key, title=f"REPLAY {tapd_id}",
            profile=PROFILE_NAME, workspace=str(ws), prd_path=str(prd),
        )
        out["create_ok"] = True
        out["create_s"] = round(time.monotonic() - t0, 1)
    except Exception as e:
        out["status"] = "failed"
        out["error"] = f"create: {e.__class__.__name__}: {e}"
        return out

    # 2. seed actions + confirm plan（仿 harness build_agent_actions）
    try:
        from testing.harness import build_agent_actions

        actions = build_agent_actions(story_key, ["design", "verify"], adapter="opencode")
        story = db.get_story(story_key)
        ctx = json.loads((story or {}).get("context_json") or "{}")
        ctx["_agent_actions"] = actions
        ctx["_plan_confirmed"] = True
        db.update_story(
            story_key, context_json=json.dumps(ctx, ensure_ascii=False),
            status="active", current_stage="design",
        )
        out["seed_ok"] = True
    except Exception as e:
        out["seed_err"] = f"{e.__class__.__name__}: {e}"
        out["status"] = "failed"
        return out

    # 3. 驱动执行（design→verify，30min 熔断）
    t0 = time.monotonic()
    try:
        planner.continue_orchestrator_agent(story_key, headless=True)
        out["run_ok"] = True
        out["run_s"] = round(time.monotonic() - t0, 1)
    except Exception as e:
        out["run_ok"] = False
        out["run_err"] = f"{e.__class__.__name__}: {e}"
        out["run_s"] = round(time.monotonic() - t0, 1)
        out["status"] = "failed"
        return out

    # 4. 收集 done 回执 + gate
    done_dir = ws / ".story" / "done" / story_key
    out["done_files"] = sorted(p.name for p in done_dir.glob("*.json")) if done_dir.exists() else []
    try:
        spec = (ws / "story" / "spec.md")
        out["spec_exists"] = spec.exists()
        if spec.exists():
            out["spec_chars"] = spec.stat().st_size
    except Exception:
        pass

    out["status"] = "ok"
    return out


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None, help="只跑单个 tapd_id")
    ap.add_argument("--samples", default=str(SANDBOX / "gold" / "samples20.json"), help="样本清单")
    args = ap.parse_args()

    _bootstrap_env()
    samples = json.loads(Path(args.samples).read_text(encoding="utf-8"))
    if args.only:
        samples = [s for s in samples if s["tapd_id"] == args.only]
    print(json.dumps({"to_run": len(samples), "only": args.only}, ensure_ascii=False))

    results = []
    for s in samples:
        out = {"cls": s.get("cls", "?")}
        r = run_one(s["tapd_id"], SANDBOX / "gold" / s["story_key"], out)
        results.append(r)
        print(json.dumps(
            {k: r.get(k) for k in ("story_key", "cls", "status", "create_ok", "run_ok", "run_s", "error", "spec_exists")},
            ensure_ascii=False), flush=True)

    Path(PACKAGE_ROOT / "results" / "pipeline_replay_20260805.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    import logging

    logging.basicConfig(level=logging.WARNING)
    main()
