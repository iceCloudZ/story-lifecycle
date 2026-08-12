"""回放回归 — 用 gold PRD 驱动真实 opencode 跑 story,收集落地 artifacts。

对 replay_set.json 里每个 story 串行执行:
1. 临时 STORY_HOME(隔离 DB,不碰生产库)
2. ``testing.workspace.reset_workspace`` 复位工作区
3. 动态生成 ``eval-replay`` profile(cli: opencode / headless / hidden,按原 profile
   阶段结构调整)写入 ``<cwd>/.story/profiles/eval-replay.yaml``
4. ``testing.harness.run_real_story`` 驱动(adapter=opencode)
5. 收集落地 artifacts → ``results/replay_<YYYYMMDD>/<story_key>/``
6. 单 story 超时 30min 熔断;失败记录原因继续下一个

注意:此任务真实驱动 opencode CLI 跑代码,执行前必须先经用户确认。
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

from . import dataset

log = logging.getLogger("eval.replay")

PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent
RESULTS_DIR = PACKAGE_ROOT / "results"
REPLAY_SET = PACKAGE_ROOT / "replay_set.json"
PROFILE_NAME = "eval-replay"
STORY_TIMEOUT_SECONDS = 30 * 60


def load_replay_set(path: str | Path | None = None) -> list[dict]:
    p = Path(path) if path else REPLAY_SET
    if not p.exists():
        raise FileNotFoundError(f"回放集不存在: {p}（先写 replay_set.json）")
    data = json.loads(p.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else data.get("stories", [])


def _write_replay_profile(stages: list[str]) -> str:
    """把 eval-replay profile 写入 cwd/.story/profiles/（project-local 优先）。"""
    stage_lines = []
    order = 0
    artifacts = {
        "design": ["story/*/spec.md"],
        "implement": ["git"],
        "build": ["git"],
        "verify": ["story/*/test-report.md"],
    }
    for name in stages:
        order += 1
        arts = artifacts.get(name, ["story/spec.md" if name == "design" else "git"])
        stage_lines.append(
            f"  {name}:\n"
            f"    order: {order}\n"
            f'    description: "eval 回放:{name}"\n'
            f"    cli: opencode\n"
            f"    confirm: false\n"
            f"    review: false\n"
            f"    max_retries: 0\n"
            f"    expected_outputs: []\n"
            f"    artifacts:\n"
            + "\n".join(f"      - {a}" for a in arts)
            + f"\n    next_default: {json.dumps(stages[order:], ensure_ascii=False)}\n"
        )
    yaml_text = (
        "# eval 回放专用 profile（replay.py 动态生成,隐藏不对外）\n"
        "version: 2\n"
        "cli: opencode\n"
        "execution_mode: headless\n"
        "hidden: true\n\n"
        "stages:\n" + "\n".join(stage_lines) + "\n"
        "quality:\n  enabled: false\n\n"
        "adversarial:\n  enabled: false\n"
    )
    profiles_dir = Path.cwd() / ".story" / "profiles"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    target = profiles_dir / f"{PROFILE_NAME}.yaml"
    target.write_text(yaml_text, encoding="utf-8")
    return str(target)


def _run_harness_with_timeout(
    workspace: str, story_key: str, prd_path: Path, stages: list[str]
) -> tuple[dict[str, Any] | None, str | None]:
    """在守护线程里跑 run_real_story;超时返回 (None, reason)。"""
    result: dict[str, Any] = {}
    exc_box: list[Exception] = []

    def worker():
        try:
            from testing.harness import run_real_story

            r = run_real_story(
                workspace=workspace,
                story_key=story_key,
                prd_path=str(prd_path),
                stages=stages,
                profile=PROFILE_NAME,
                title=f"EVAL REPLAY {story_key}",
                adapter="opencode",
                headless=True,
                check_ai_cli=True,
            )
            result["final_story"] = r.final_story or {}
            result["stages"] = [
                {"stage": s.stage, "done": s.done_file.exists(), "error": s.error} for s in r.stages
            ]
        except Exception as e:  # noqa: BLE001
            exc_box.append(e)

    t = threading.Thread(target=worker, daemon=True, name=f"replay-{story_key}")
    t.start()
    t.join(timeout=STORY_TIMEOUT_SECONDS)
    if t.is_alive():
        return None, f"timeout({STORY_TIMEOUT_SECONDS}s)"
    if exc_box:
        return None, f"{exc_box[0].__class__.__name__}: {exc_box[0]}"
    if not result:
        return None, "harness returned empty result"
    return result, None


def _git_state(workspace: str) -> dict[str, str]:
    """收集工作区 git 状态摘要（diff stat + status 摘要）。"""
    out: dict[str, str] = {}
    for label, cmd in (
        ("git_status", ["git", "status", "--porcelain"]),
        ("git_diff_stat", ["git", "diff", "HEAD", "--stat"]),
    ):
        try:
            r = subprocess.run(
                cmd, cwd=workspace, capture_output=True, text=True, timeout=30, encoding="utf-8"
            )
            out[label] = r.stdout[:40_000] or ""
        except Exception as e:  # noqa: BLE001
            out[label] = f"(error: {e})"
    return out


def _collect_artifacts(
    workspace: str, story_key: str, stages: list[str], out_dir: Path
) -> dict[str, str]:
    """收集落地 artifacts 到 out_dir,返回 {名称: 文本} 供 ConformanceScore。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    texts: dict[str, str] = {}
    ws = Path(workspace)
    for fname in ("spec.md", "plan.md", "test-report.md", "research.md"):
        # opencode 落地到 story/<story_key>-EVALREPLAY10/<fname>（子目录），用 glob 匹配 + 兜底 story/<fname>
        hits = list(ws.glob(f"story/*/{fname}"))
        fallback = ws / "story" / fname
        if fallback.exists() and fallback.stat().st_size:
            hits.append(fallback)
        p = next((h for h in hits if h.exists() and h.stat().st_size), None)
        if p:
            dst = out_dir / p.name
            shutil.copy2(p, dst)
            texts[f"story/{fname}"] = dataset._read_text_robust(p)
    git = _git_state(workspace)
    for label, text in git.items():
        if text:
            texts[label] = text
    (out_dir / "git_state.json").write_text(
        json.dumps(git, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # done 回执
    for stage in stages:
        rec = ws / ".story" / "done" / dataset._safe_segment(story_key) / f"{stage}.json"
        if rec.exists():
            shutil.copy2(rec, out_dir / f"done-{stage}.json")
    return texts


def run_replay(
    results_dir: str | Path | None = None,
    only: str | None = None,
) -> dict[str, Any]:
    """执行回放集;返回汇总 dict。only=story_key 只跑单个。"""
    replay_set = load_replay_set()
    res_dir = Path(results_dir) if results_dir else RESULTS_DIR
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = res_dir / f"replay_{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)

    from .judges import configure_llm_env

    configure_llm_env()  # 管线内 LLM（planner/gate）同走 Go 端点

    outcomes: list[dict[str, Any]] = []
    for i, entry in enumerate(replay_set, 1):
        key = entry["story_key"]
        if only and key != only:
            continue
        workspace = entry.get("workspace", "")
        stages = entry.get("stages", ["design", "implement", "verify"])
        log.info("[%d/%d] 回放 %s（stages=%s）", i, len(replay_set), key, stages)
        story_out_dir = run_dir / dataset._safe_segment(key)
        if story_out_dir.exists():
            shutil.rmtree(story_out_dir, ignore_errors=True)
        story_out_dir.mkdir(parents=True, exist_ok=True)

        entry_out = {
            "story_key": key,
            "workspace": workspace,
            "stages": stages,
            "reason": entry.get("reason", ""),
        }
        try:
            # 1. 临时 STORY_HOME 隔离 DB
            tmp_home = Path(tempfile.mkdtemp(prefix=f"eval_{dataset._safe_segment(key)}_"))
            os.environ["STORY_HOME"] = str(tmp_home)
            log.info("STORY_HOME=%s", tmp_home)

            # 1.5 初始化隔离 DB 的表结构（临时 STORY_HOME 是空库，必须建表）
            from story_lifecycle.infra.db.schema import init_db
            init_db()

            # 2. 复位工作区
            from testing.workspace import reset_workspace

            reset_workspace(workspace, key)

            # 3. 动态 profile
            profile_path = _write_replay_profile(stages)
            entry_out["profile"] = profile_path

            # 4. gold PRD（来自 dataset）
            ds_dir = dataset.DATASET_DIR
            manifests = {m["story_key"]: m for m in dataset.load_manifests(ds_dir)}
            mf = manifests.get(key)
            prd_path = dataset.artifact_path(ds_dir, mf, "prd") if mf else None
            if not prd_path:
                raise RuntimeError(f"gold PRD 缺失（dataset 里没有 {key}）")

            # 5. 驱动真实回放（30min 熔断）
            result, err = _run_harness_with_timeout(workspace, key, prd_path, stages)
            if err:
                entry_out["status"] = "failed"
                entry_out["error"] = err
                (story_out_dir / "failure.txt").write_text(err, encoding="utf-8")
                log.error("回放 %s 失败: %s", key, err)
                outcomes.append(entry_out)
                continue

            # 6. 收集落地 artifacts
            texts = _collect_artifacts(workspace, key, stages, story_out_dir)
            entry_out.update(
                {
                    "status": "ok",
                    "final_story": {
                        "status": (result or {}).get("final_story", {}).get("status"),
                        "current_stage": (result or {}).get("final_story", {}).get("current_stage"),
                    },
                    "artifacts": sorted(texts.keys()),
                    "stage_results": (result or {}).get("stages", []),
                }
            )
            # 回执目录也带进 json（留给 report 用）
            (story_out_dir / "replay_result.json").write_text(
                json.dumps(entry_out, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            log.info("回放 %s 完成: %s", key, entry_out["final_story"])
        except Exception as e:  # noqa: BLE001
            entry_out["status"] = "failed"
            entry_out["error"] = f"{e.__class__.__name__}: {e}"
            (story_out_dir / "failure.txt").write_text(str(e), encoding="utf-8")
            log.exception("回放 %s 异常", key)
        outcomes.append(entry_out)

    summary = {
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "count": len(outcomes),
        "ok": sum(1 for o in outcomes if o.get("status") == "ok"),
        "failed": [o for o in outcomes if o.get("status") != "ok"],
        "stories": outcomes,
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info("回放汇总: %s", {k: v for k, v in summary.items() if k != "stories"})
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    print(json.dumps(run_replay(), ensure_ascii=False, indent=2))
