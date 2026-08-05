"""维度一：coverage 插桩重跑（round2.5 §1）。

两类：2 条管线回放（A 类 1067103 + B 类 1067383，同沙箱分段驱动）+ 167 条 gate 回测。
source 限定 story_lifecycle。产出 results/coverage_data_20260805/ 原始数据 + 模块表。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent
SANDBOX = PACKAGE_ROOT / "sandbox"
RESULTS = PACKAGE_ROOT / "results"
DATA = RESULTS / "coverage_data_20260805"
SL_SRC = PACKAGE_ROOT.parent / "packages" / "story-lifecycle" / "src"

TARGETS = ["story_lifecycle"]


def _run_under_coverage(argv: list[str], data_file: str, cwd: Path) -> None:
    """用 coverage 跑一个子进程，数据落指定文件。"""
    cov = ["coverage", "run", "--source=story_lifecycle", "--data-file", str(data_file)]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SL_SRC) + os.pathsep + env.get("PYTHONPATH", "")
    r = subprocess.run(cov + argv, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=3600, env=env)
    if r.returncode != 0:
        print(f"  [warn] {argv[0]} exit={r.returncode}: {r.stderr[-300:]}", flush=True)


def main() -> dict:
    DATA.mkdir(parents=True, exist_ok=True)
    os.environ["STORY_HOME"] = str(SANDBOX / "story_home")
    os.environ["STORY_LLM_BASE_URL"] = "https://opencode.ai/zen/go/v1"

    # 1. 2 条管线回放（分段驱动）
    replay_taps = ["1144381896001067103", "1144381896001067383"]  # A 类 + B 类
    for tid in replay_taps:
        print(f"插桩回放 {tid}...", flush=True)
        _run_under_coverage(
            ["-m", "eval.pipeline_replay", "--only", tid],
            str(DATA / f"cov_replay_{tid}.coverage"),
            cwd=PACKAGE_ROOT,
        )

    # 2. 167 条 gate 回测（gate_replay main）
    print("插桩 gate 回测 167 条...", flush=True)
    _run_under_coverage(
        ["-c", "import sys; sys.path.insert(0,'src'); from eval.gate_replay import main; main()"],
        str(DATA / "cov_gate_replay.coverage"),
        cwd=PACKAGE_ROOT,
    )

    # 3. 合并 + 按模块汇总
    cov_files = list(DATA.glob("*.coverage"))
    combined = DATA / "combined.coverage"
    if combined.exists():
        combined.unlink()
    for f in cov_files:
        subprocess.run(["coverage", "combine", "--data-file", str(combined), str(f)],
                       capture_output=True, text=True, timeout=120)
    subprocess.run(["coverage", "report", "--data-file", str(combined), "--format=json",
                    "-o", str(DATA / "combined.json")], capture_output=True, text=True, timeout=120)

    data = json.loads((DATA / "combined.json").read_text(encoding="utf-8"))
    files = data.get("files", {})
    # 按模块聚合
    mods = {}
    for path, info in files.items():
        if "story_lifecycle" not in path:
            continue
        rel = path.split("story_lifecycle/", 1)[-1]
        parts = rel.split("/")
        mod = "/".join(parts[:2]) if len(parts) > 1 else parts[0]
        s = info.get("summary", {})
        mods.setdefault(mod, {"num": 0, "covered": 0, "missed": 0, "branches": [0, 0]})
        mods[mod]["num"] += 1
        mods[mod]["covered"] += s.get("covered_lines", 0)
        mods[mod]["missed"] += s.get("missing_lines", 0)
        mods[mod]["branches"][0] += s.get("covered_branches", 0)
        mods[mod]["branches"][1] += s.get("num_branches", 0)

    report = {
        "total_files": len(files),
        "modules": {},
    }
    for mod in sorted(mods):
        m = mods[mod]
        line_pct = m["covered"] / (m["covered"] + m["missed"]) * 100 if (m["covered"] + m["missed"]) else 0
        br_pct = m["branches"][0] / m["branches"][1] * 100 if m["branches"][1] else 0
        report["modules"][mod] = {
            "files": m["num"], "line_covered": m["covered"], "line_missed": m["missed"],
            "line_pct": round(line_pct, 1), "branch_pct": round(br_pct, 1),
        }

    # 4. 重点文件明细（planner/graph/unified_gate/stage_completion）
    key_files = {}
    for path, info in files.items():
        if any(k in path for k in ("planner.py", "graph.py", "unified_gate.py", "stage_completion.py",
                                   "prd_generator.py", "api.py")):
            s = info.get("summary", {})
            key_files[path.split("story_lifecycle/", 1)[-1]] = {
                "line_pct": round(s.get("percent_covered", 0), 1),
                "covered_lines": s.get("covered_lines", 0),
                "missing_lines": s.get("missing_lines", 0),
                "branch_pct": round(s.get("percent_covered_branches", 0), 1) if s.get("num_branches") else None,
            }
    report["key_files"] = key_files

    (DATA / "module_summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING)
    main()
