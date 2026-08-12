"""UI-driven eval(差分 path B)— 走 serve HTTP API 跑 gold story。

和 in-process eval(path A)同 gold + 同 profile(eval-replay)+ 同 judge(judge_spec)。
差异只在 spawn 驱动:
  - path A = continue_orchestrator_agent(force_auto) 总 spawn
  - path B = serve 编排线程 make_stage_executor → AutomaticStageExecutor
差分 judge_A - judge_B 量化 serve 调度层影响(用 path A 当 oracle 测 path B)。

**UI 轨 bug 记录**:每个 story 跑出详细 events 时间线(每步 ts + 状态 + 异常),
写进 result.json 的 events[] + 详细 log 输出,便于定位 UI/serve 轨的 bug 与不一致。
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import shutil
import time
import traceback
from pathlib import Path
from typing import Any

import httpx

from . import dataset, judges
from .replay import RESULTS_DIR, load_replay_set

log = logging.getLogger("eval.ui_replay")

PROFILE = "eval-replay"  # 原生 AUTO_PROFILES → serve 编排线程自动 spawn
POLL_INTERVAL = 8
POLL_TIMEOUT = 1500  # flash 设计阶段真实耗时 8~15min 方差(2026-08-11 服务器实测),600s 会把慢轮误判"停滞"
FULL_TIMEOUT = 5400  # 全流程(design+implement+verify+lifecycle gates)~30-45min,留足 90min


def _evt(proc: dict, step: str, **kw: Any) -> None:
    """记录 UI 轨事件时间线(进 result.json events[],便于 bug 追溯)。"""
    proc["events"].append({"ts": round(time.time(), 2), "step": step, **kw})


class _ServeClient:
    def __init__(self, base_url: str, timeout: float = 300):
        self.client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)

    def create_story(self, key, title, profile, workspace, autostart=False):
        r = self.client.post("/api/story", json={
            "key": key, "title": title, "profile": profile,
            "workspace": workspace, "autostart": autostart,
        })
        r.raise_for_status()
        return r.json()

    def start(self, key, content):
        r = self.client.post(f"/api/story/{key}/start", json={
            "content": content, "project_ids": [], "seed_context": "", "branch": "",
        })
        r.raise_for_status()
        return r.json()

    def confirm_plan(self, key):
        r = self.client.post(f"/api/story/{key}/plan/confirm", json={})
        r.raise_for_status()
        return r.json()

    def get_story(self, key):
        r = self.client.get(f"/api/story/{key}")
        r.raise_for_status()
        return r.json()

    def get_sessions(self, key):
        r = self.client.get(f"/api/story/{key}/sessions")
        r.raise_for_status()
        return r.json()

    def advance_lifecycle(self, key):
        """POST /lifecycle/advance —— 续推 lifecycle confirm-gate(开发→测试 等 ui_button 状态)。

        全自动 eval 用:story 在状态闸 paused 等人确认时,驱动层自动续推,让
        design→implement→verify→done 全自动跑通(编排器的 gate 是给真人 story 的安全网,
        eval 测试 story 自动绕过合理)。2026-08-12:FULLTEST 实测 implement approve 后卡在
        开发→测试 闸,POST advance 即续推到 verify。
        """
        r = self.client.post(f"/api/story/{key}/lifecycle/advance")
        r.raise_for_status()
        return r.json()

    def delete_story(self, key):
        try:
            self.client.delete(f"/api/story/{key}")
        except Exception:
            pass


def _spec_landed(workspace: str, prefer_after: float | None = None) -> Path | None:
    ws = Path(workspace)
    cands = [p for p in ws.glob("story/*/spec.md") if p.exists() and p.stat().st_size]
    fb = ws / "story" / "spec.md"
    if fb.exists() and fb.stat().st_size:
        cands.append(fb)
    if not cands:
        return None
    if prefer_after is not None:
        recent = [p for p in cands if p.stat().st_mtime >= prefer_after]
        return max(recent, key=lambda p: p.stat().st_mtime) if recent else None
    return max(cands, key=lambda p: p.stat().st_mtime)


def _gold_prd(key: str) -> str:
    manifests = {m["story_key"]: m for m in dataset.load_manifests(str(dataset.DATASET_DIR))}
    mf = manifests.get(key)
    if not mf:
        raise RuntimeError(f"gold manifest 缺失: {key}")
    pp = dataset.artifact_path(dataset.DATASET_DIR, mf, "prd")
    return dataset._read_text_robust(pp) if pp else ""


def _template_text() -> str:
    env = os.environ.get("EVAL_SPEC_TEMPLATE")
    if env and Path(env).exists():
        return Path(env).read_text(encoding="utf-8")
    return ""


def run_ui_replay(serve_url: str = "http://localhost:8180",
                  results_dir: str | Path | None = None,
                  only: str | None = None) -> dict[str, Any]:
    """走 serve API 跑 replay_set 的 gold story(path B),详细记录 UI 轨 events。"""
    judges.configure_llm_env()
    replay_set = load_replay_set()
    res_dir = Path(results_dir) if results_dir else RESULTS_DIR
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = res_dir / f"ui_replay_{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)

    client = _ServeClient(serve_url)
    template = _template_text()
    outcomes: list[dict[str, Any]] = []

    for i, entry in enumerate(replay_set, 1):
        key = entry["story_key"]
        if only and key != only:
            continue
        workspace = entry["workspace"]
        log.info("=" * 60)
        log.info("[%d/%d] UI 回放 %s (workspace=%s)", i, len(replay_set), key, workspace)
        story_out = run_dir / dataset._safe_segment(key)
        if story_out.exists():
            shutil.rmtree(story_out, ignore_errors=True)
        story_out.mkdir(parents=True, exist_ok=True)

        ui_key = f"UI-{key}-{int(time.time()) % 100000}"
        proc: dict[str, Any] = {
            "story_key": key, "ui_key": ui_key, "workspace": workspace,
            "spawn_triggered": False, "final_status": None, "final_stage": None,
            "paused": False, "pause_reason": None, "lastError": None,
            "spec_produced": False, "spec_score": None, "error": None,
            "elapsed_s": 0, "events": [], "anomalies": [],
        }
        t0 = time.time()
        _evt(proc, "start", key=key, workspace=workspace)
        try:
            gold_prd = _gold_prd(key)
            _evt(proc, "gold_prd_loaded", len=len(gold_prd))

            # create
            try:
                client.create_story(ui_key, title=key, profile=PROFILE, workspace=workspace, autostart=False)
                _evt(proc, "create_story", ok=True)
                log.info("[%s] create_story ok", key)
            except Exception as e:
                _evt(proc, "create_story", ok=False, err=str(e))
                raise

            # start (gold PRD 注入 + planner)
            try:
                client.start(ui_key, content=gold_prd)
                _evt(proc, "start", ok=True)
                log.info("[%s] start ok (PRD + planner)", key)
            except Exception as e:
                _evt(proc, "start", ok=False, err=str(e))
                raise

            # confirm
            try:
                client.confirm_plan(ui_key)
                _evt(proc, "confirm", ok=True)
                log.info("[%s] confirm ok → 编排线程接管", key)
            except Exception as e:
                _evt(proc, "confirm", ok=False, err=str(e))
                raise

            # 轮询
            poll_count = 0
            while time.time() - t0 < POLL_TIMEOUT:
                poll_count += 1
                try:
                    st = client.get_story(ui_key)
                except Exception as e:
                    _evt(proc, "poll_get_failed", poll=poll_count, err=str(e))
                    proc["anomalies"].append(f"poll#{poll_count} GET story failed: {e}")
                    time.sleep(POLL_INTERVAL)
                    continue
                raw_ctx = st.get("contextJson")
                ctx = json.loads(raw_ctx) if isinstance(raw_ctx, str) else (raw_ctx or {})
                done_stages = ctx.get("_completed_stages") or []
                status, stage = st.get("status"), st.get("currentStage")
                proc["final_status"], proc["final_stage"] = status, stage
                proc["paused"] = (status == "paused")
                proc["pause_reason"] = ctx.get("_pause_reason")
                proc["lastError"] = st.get("lastError")
                # spawn 探测(诊断用:sessions 对 headless 不可靠,见 anomaly)
                sess_rows = []
                try:
                    sess = client.get_sessions(ui_key)
                    sess_rows = sess.get("sessions", []) if isinstance(sess, dict) else []
                except Exception:
                    pass
                spec_p = _spec_landed(workspace, t0)
                if spec_p:
                    proc["spec_produced"] = True
                    proc["spawn_triggered"] = True  # headless spawn 不在 /sessions,spec 落地即 spawn 成功
                # 异常记录
                if status == "paused" and poll_count == 1:
                    proc["anomalies"].append(f"poll#{poll_count} paused 早(刚 confirm 就 paused,reason={ctx.get('_pause_reason')})")
                if proc["lastError"]:
                    proc["anomalies"].append(f"poll#{poll_count} lastError={st.get('lastError')}")
                if poll_count <= 3 or poll_count % 5 == 0:
                    _evt(proc, "poll", n=poll_count, status=status, stage=stage,
                         spawn=proc["spawn_triggered"], spec=proc["spec_produced"],
                         sessions_n=len(sess_rows))
                # 终止
                finished = (
                    "design" in done_stages
                    or stage not in (None, "design")
                    or status in ("paused", "failed", "completed")
                    or (spec_p and proc["spawn_triggered"])
                )
                if finished:
                    _evt(proc, "poll_done", n=poll_count, reason="finished" if spec_p or status in ("paused","failed","completed") or stage not in (None,"design") else "stage_done")
                    log.info("[%s] 轮询结束 poll#%d status=%s stage=%s spec=%s", key, poll_count, status, stage, proc["spec_produced"])
                    break
                time.sleep(POLL_INTERVAL)
            else:
                _evt(proc, "poll_done", n=poll_count, reason="timeout")
                proc["anomalies"].append(f"轮询超时({POLL_TIMEOUT}s)未完成: final status={proc['final_status']} stage={proc['final_stage']} spawn={proc['spawn_triggered']}")
                log.warning("[%s] 轮询超时", key)

            # collect + judge
            spec_p = _spec_landed(workspace, t0)
            if spec_p:
                shutil.copy2(spec_p, story_out / "spec.md")
                spec_b = dataset._read_text_robust(spec_p)
                proc["spec_produced"] = True
                proc["spawn_triggered"] = True
                score = judges.judge_spec(gold_prd, spec_b, template)
                proc["spec_score"] = score.model_dump()
                _evt(proc, "judge", score=proc["spec_score"])
                log.info("[%s] judge_B=%s", key, {k: proc["spec_score"].get(k) for k in ["completeness", "template_compliance", "acceptability"]})
            else:
                _evt(proc, "judge", skipped="no spec")
                proc["anomalies"].append("无 spec 产出 → 无法 judge(spawn 未触发 或 opencode 未写 spec)")
        except Exception as e:  # noqa: BLE001
            proc["error"] = f"{e.__class__.__name__}: {e}"
            proc["anomalies"].append(f"异常: {e}\n{traceback.format_exc()[-400:]}")
            _evt(proc, "exception", err=proc["error"])
            log.exception("[%s] UI 回放异常", key)
        finally:
            proc["elapsed_s"] = round(time.time() - t0, 1)
            _evt(proc, "delete", ui_key=ui_key)
            client.delete_story(ui_key)

        (story_out / "result.json").write_text(
            json.dumps(proc, ensure_ascii=False, indent=2), encoding="utf-8")
        outcomes.append(proc)
        log.info("[%s] UI 回放汇总: spawn=%s spec=%s paused=%s anomalies=%d (%.1fs)",
                 key, proc["spawn_triggered"], proc["spec_produced"], proc["paused"],
                 len(proc["anomalies"]), proc["elapsed_s"])
        for a in proc["anomalies"]:
            log.warning("[%s] ANOMALY: %s", key, a)

    summary = {
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "path": "B (serve)", "count": len(outcomes),
        "ok": sum(1 for o in outcomes if o.get("spec_produced")),
        "failed": [o for o in outcomes if not o.get("spec_produced")],
        "total_anomalies": sum(len(o.get("anomalies", [])) for o in outcomes),
        "stories": outcomes,
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def run_ui_full_lifecycle(serve_url: str = "http://localhost:8180",
                           results_dir: str | Path | None = None,
                           only: str | None = None) -> dict[str, Any]:
    """全自动全流程 eval:path B(serve)跑 design→implement→verify→done,遇 lifecycle
    confirm-gate(开发→测试、测试→done 等 ui_button 状态)自动 POST /lifecycle/advance 续推。

    区别于 run_ui_replay(design-only,出 spec 就 judge+删):本函数跑到终态(completed/failed),
    记录各 stage 推进 + auto-advance 次数 + 最终结果,用于验证全流程能跑通(各 stage 质量由
    serve 侧 judge 判,本函数只观测 + 续推 gate)。诊断来源(2026-08-12):implement approve 后
    卡在 开发→测试 闸,非 stage 失败、非模型能力,纯 lifecycle gate。
    """
    judges.configure_llm_env()
    replay_set = load_replay_set()
    res_dir = Path(results_dir) if results_dir else RESULTS_DIR
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = res_dir / f"ui_full_{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)

    client = _ServeClient(serve_url)
    outcomes: list[dict[str, Any]] = []

    for i, entry in enumerate(replay_set, 1):
        key = entry["story_key"]
        if only and key != only:
            continue
        workspace = entry["workspace"]
        log.info("=" * 60)
        log.info("[%d/%d] UI 全流程 %s (workspace=%s)", i, len(replay_set), key, workspace)
        story_out = run_dir / dataset._safe_segment(key)
        if story_out.exists():
            shutil.rmtree(story_out, ignore_errors=True)
        story_out.mkdir(parents=True, exist_ok=True)

        ui_key = f"UI-{key}-{int(time.time()) % 100000}"
        proc: dict[str, Any] = {
            "story_key": key, "ui_key": ui_key, "workspace": workspace,
            "final_status": None, "final_stage": None, "completed_stages": [],
            "advances": 0, "stage_trace": [], "error": None,
            "elapsed_s": 0, "anomalies": [],
        }
        t0 = time.time()
        try:
            gold_prd = _gold_prd(key)
            client.create_story(ui_key, title=key, profile=PROFILE, workspace=workspace, autostart=False)
            client.start(ui_key, content=gold_prd)
            client.confirm_plan(ui_key)
            log.info("[%s] created+started+confirmed → 全流程开始", key)

            poll_count = 0
            last_pause_stage = None  # guard:同 stage 反复 paused 不无限 advance
            status, stage, done = None, None, []
            while time.time() - t0 < FULL_TIMEOUT:
                poll_count += 1
                try:
                    st = client.get_story(ui_key)
                except Exception as e:
                    proc["anomalies"].append(f"poll#{poll_count} GET failed: {e}")
                    time.sleep(POLL_INTERVAL)
                    continue
                raw_ctx = st.get("contextJson")
                ctx = json.loads(raw_ctx) if isinstance(raw_ctx, str) else (raw_ctx or {})
                done = ctx.get("_completed_stages") or []
                status, stage = st.get("status"), st.get("currentStage")
                proc["final_status"], proc["final_stage"], proc["completed_stages"] = status, stage, done
                if not proc["stage_trace"] or proc["stage_trace"][-1].get("stage") != stage or proc["stage_trace"][-1].get("status") != status:
                    proc["stage_trace"].append({"poll": poll_count, "stage": stage, "status": status, "done": list(done)})
                if poll_count <= 3 or poll_count % 6 == 0:
                    log.info("[%s] poll#%d status=%s stage=%s done=%s", key, poll_count, status, stage, done)
                # auto-advance:paused 在 confirm-gate → 续推。guard 防同 stage 连续 advance
                if status == "paused":
                    if stage != last_pause_stage:
                        last_pause_stage = stage
                        try:
                            client.advance_lifecycle(ui_key)
                            proc["advances"] += 1
                            log.info("[%s] auto-advance #%d (paused @ %s)", key, proc["advances"], stage)
                        except Exception as e:
                            proc["anomalies"].append(f"advance failed @ {stage}: {e}")
                if status in ("completed", "failed"):
                    log.info("[%s] 终态 status=%s stage=%s done=%s (advances=%d)", key, status, stage, done, proc["advances"])
                    break
                time.sleep(POLL_INTERVAL)
            else:
                proc["anomalies"].append(f"全流程超时({FULL_TIMEOUT}s): final {status}@{stage} done={done}")
                log.warning("[%s] 全流程超时", key)
        except Exception as e:  # noqa: BLE001
            proc["error"] = f"{e.__class__.__name__}: {e}"
            proc["anomalies"].append(f"异常: {e}\n{traceback.format_exc()[-400:]}")
            log.exception("[%s] 全流程异常", key)
        finally:
            proc["elapsed_s"] = round(time.time() - t0, 1)
            client.delete_story(ui_key)

        (story_out / "result.json").write_text(
            json.dumps(proc, ensure_ascii=False, indent=2), encoding="utf-8")
        outcomes.append(proc)
        log.info("[%s] 全流程汇总: final=%s@%s done=%s advances=%d anomalies=%d (%.1fs)",
                 key, proc["final_status"], proc["final_stage"], proc["completed_stages"],
                 proc["advances"], len(proc["anomalies"]), proc["elapsed_s"])

    summary = {
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "path": "B-full (serve full lifecycle)", "count": len(outcomes),
        "completed": sum(1 for o in outcomes if o.get("final_status") == "completed"),
        "stories": outcomes,
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def run_diff(results_dir: str | Path | None = None) -> dict[str, Any]:
    """读 path A replay_<date> + path B ui_replay_<date>,每 story 各 judge spec,出 delta 报告。"""
    judges.configure_llm_env()
    res_dir = Path(results_dir) if results_dir else RESULTS_DIR
    a_runs = sorted(res_dir.glob("replay_*"))
    b_runs = sorted(res_dir.glob("ui_replay_*"))
    if not a_runs or not b_runs:
        raise RuntimeError(f"缺 path A replay 或 path B ui_replay (results_dir={res_dir})")
    path_a, path_b = a_runs[-1], b_runs[-1]
    template = _template_text()
    dims = ["completeness", "template_compliance", "acceptability"]

    rows = []
    for story_dir in sorted(path_b.iterdir()):
        if not story_dir.is_dir():
            continue
        key = story_dir.name
        b_spec = story_dir / "spec.md"
        a_spec = path_a / key / "spec.md"
        b_result = json.loads((story_dir / "result.json").read_text(encoding="utf-8")) if (story_dir / "result.json").exists() else {}

        gold_prd = ""
        try:
            gold_prd = _gold_prd(key)
        except Exception:
            pass
        sa = dataset._read_text_robust(a_spec) if a_spec.exists() else ""
        sb = dataset._read_text_robust(b_spec) if b_spec.exists() else ""
        ja = judges.judge_spec(gold_prd, sa, template).model_dump() if sa else None
        jb = judges.judge_spec(gold_prd, sb, template).model_dump() if sb else None
        rows.append({"key": key, "ja": ja, "jb": jb,
                     "b_spawn": b_result.get("spawn_triggered"),
                     "b_paused": b_result.get("paused"),
                     "b_error": b_result.get("error"),
                     "b_anomalies": b_result.get("anomalies", [])})

    lines = [
        f"# 差分报告 path A (in-process) vs path B (serve) {_dt.date.today().strftime('%Y%m%d')}",
        "", f"- path A: `{path_a.name}`(in-process continue_orchestrator_agent, force_auto 总 spawn)",
        f"- path B: `{path_b.name}`(serve 编排线程 make_stage_executor)",
        "", "## spec judge 差分(delta = A − B;正=serve 落后 oracle 🔴 / 负=serve 更好 🟢 / 0=相当)", "",
        "| story | 维度 | path A | path B | delta |",
        "|-------|------|--------|--------|-------|",
    ]
    regressions = []
    for r in rows:
        for d in dims:
            a = (r["ja"] or {}).get(d)
            b = (r["jb"] or {}).get(d)
            if a is None and b is None:
                continue
            a_s = str(a) if a is not None else "—"
            b_s = str(b) if b is not None else "—(无产出)"
            delta = (a - b) if (a is not None and b is not None) else None
            mark = " 🔴" if (delta is not None and delta >= 1) else (" 🟢" if (delta is not None and delta <= -1) else "")
            delta_s = (f"{delta:+d}{mark}") if delta is not None else "—"
            lines.append(f"| {r['key']} | spec.{d} | {a_s} | {b_s} | {delta_s} |")
            if delta is not None and delta >= 1:
                regressions.append(f"{r['key']} spec.{d}: A={a} B={b} (serve 落后 {delta:+d})")

    lines += ["", "## 流程差异(UI 轨 bug/异常记录)", "",
              "| story | A spawn | B spawn | B paused | B anomalies | B error |",
              "|-------|---------|---------|----------|-------------|---------|"]
    for r in rows:
        lines.append(f"| {r['key']} | ✓(force_auto) | {'✓' if r['b_spawn'] else '✗'} | "
                     f"{'是' if r['b_paused'] else '否'} | {len(r['b_anomalies'])} | {str(r['b_error'])[:30] or '—'} |")

    # 详细 anomaly 明细
    any_anom = any(r["b_anomalies"] for r in rows)
    if any_anom:
        lines += ["", "## ⚠ UI 轨异常明细(path B bug 线索)", ""]
        for r in rows:
            for a in r["b_anomalies"]:
                lines.append(f"- **{r['key']}**: {a}")

    if regressions:
        lines += ["", "## 🔴 serve 落后 oracle(质量层退化)", ""]
        lines += [f"- {x}" for x in regressions]
    else:
        lines += ["", "_(serve 不落后 oracle:质量层无退化;若 B 有 anomaly,问题在流程层)_"]

    md = res_dir / f"diff_{_dt.date.today().strftime('%Y%m%d')}.md"
    md.write_text("\n".join(lines), encoding="utf-8")
    return {"md": str(md), "rows": len(rows),
            "regressions": len(regressions),
            "anomalies": sum(len(r["b_anomalies"]) for r in rows)}
