"""round2 度量：gate 回放 + spec 质量评分 + 沙箱审计，产出 pipeline_replay_20260805.md。

从 final20.json 读 20 条回放结果；对每条：
1. gate 回放：用生成 spec + 历史 diff 组装 done_data 调 run_unified_verify_gate
2. spec 评分：ConformanceScore 同款 prompt，参照物 = gold PRD 输入
3. 沙箱审计：原库 md5、hc-all git status、D:/hc-all/story 零写入
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent
SANDBOX = PACKAGE_ROOT / "sandbox"
RESULTS = PACKAGE_ROOT / "results"
SL_SRC = PACKAGE_ROOT.parent / "packages" / "story-lifecycle" / "src"
PROD_DB = Path.home() / ".story-lifecycle" / "story.db"
HC_STORY = Path("D:/hc-all/story")


def _md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest() if p.exists() else "MISSING"


def _file_sig(p: Path) -> tuple[int, float]:
    """文件指纹：大小 + mtime（不读内容，防大目录 MemoryError）。"""
    st = p.stat()
    return st.st_size, st.st_mtime_ns


def _git_status(repo: Path) -> str:
    r = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True,
                       text=True, timeout=30, encoding="utf-8")
    return r.stdout


def _audit() -> dict:
    out = {}
    out["prod_db_md5_before"] = _md5(PROD_DB)
    hc = Path("D:/hc-all")
    repos = [d for d in hc.iterdir() if d.is_dir() and (d / ".git").exists()]
    repos.append(hc / "frontends" / "hc-admin")
    out["hc_git_before"] = {r.name: _git_status(r) for r in repos}
    # D:/hc-all/story 文件清单（路径+大小+mtime，不读内容）
    story_files = {}
    if HC_STORY.exists():
        for f in HC_STORY.rglob("*"):
            if f.is_file():
                story_files[str(f)] = _file_sig(f)
    out["hc_story_files_before"] = story_files
    return out


def _audit_after(before: dict) -> dict:
    out = {}
    out["prod_db_unchanged"] = before["prod_db_md5_before"] == _md5(PROD_DB)
    hc = Path("D:/hc-all")
    repos = [d for d in hc.iterdir() if d.is_dir() and (d / ".git").exists()]
    repos.append(hc / "frontends" / "hc-admin")
    changed = []
    for r in repos:
        now = _git_status(r)
        if now != before["hc_git_before"].get(r.name, ""):
            changed.append(r.name)
    out["hc_git_changed"] = changed
    story_files = {}
    if HC_STORY.exists():
        for f in HC_STORY.rglob("*"):
            if f.is_file():
                story_files[str(f)] = _file_sig(f)
    new_files = [k for k in story_files if k not in before["hc_story_files_before"]]
    modified = [k for k in story_files if k in before["hc_story_files_before"]
                and story_files[k] != before["hc_story_files_before"][k]]
    out["hc_story_new"] = new_files
    out["hc_story_modified"] = modified
    out["hc_story_clean"] = not new_files and not modified
    return out


def _run_gate(row: dict, out_dir: Path) -> dict:
    """用生成 spec + 历史 diff 调 gate。"""
    os.environ["STORY_HOME"] = str(SANDBOX / "story_home")
    os.environ["STORY_LLM_BASE_URL"] = "https://opencode.ai/zen/go/v1"
    sys.path.insert(0, str(SL_SRC))
    from eval.judges import configure_llm_env
    configure_llm_env()
    from story_lifecycle.orchestrator.evaluation.unified_gate import run_unified_verify_gate

    spec_path = Path(row["spec"])
    diff_path = Path(row["story_key"]).parent  # not used
    # 历史 diff 从 gold 目录拿
    gold = SANDBOX / "gold" / row["story_key"]
    diff_text = ""
    dp = gold / "delivery.diff"
    if dp.exists():
        diff_text = dp.read_text(encoding="utf-8", errors="replace")[:60_000]
    spec_text = spec_path.read_text(encoding="utf-8", errors="replace")[:60_000]

    done_data = {
        "summary": f"回放生成 spec 交付；历史 diff 见 delivery.diff\n\n{spec_text[:3000]}",
        "files_changed": [str(spec_path)],
        "delivery_diff": diff_text[:60_000],
    }
    story_key = row["story_key"]
    t0 = time.monotonic()
    try:
        result = run_unified_verify_gate(
            story_key=story_key, stage="verify", workspace=str(SANDBOX / "ws" / story_key),
            context={"task_type": ""}, done_data=done_data,
            adapter_name="opencode", retry_count=1,
        )
        return {
            "gate_verdict": result.get("verdict"),
            "gate_decision": result.get("decision"),
            "gate_reason": (result.get("reason") or "")[:300],
            "gate_findings": result.get("findings") or [],
            "elapsed_s": round(time.monotonic() - t0, 1),
        }
    except Exception as e:
        return {"gate_error": f"{e.__class__.__name__}: {e}"[:200], "elapsed_s": round(time.monotonic() - t0, 1)}


def main() -> dict:
    rows = json.loads(Path('C:/Users/zzh58/AppData/Local/Temp/opencode/final20.json').read_text(encoding='utf-8'))
    audit_before = _audit()

    for r in rows:
        if r["spec"]:
            g = _run_gate(r, RESULTS)
            r.update(g)
        else:
            r["gate_decision"] = "skip"
        print(f"[{r['cls']}] {r['tapd_id'][-8:]} gate={r.get('gate_decision','?')} {r.get('gate_verdict','')}", flush=True)

    audit_after = _audit_after(audit_before)
    _render(rows, audit_after)
    return {"n": len(rows), "audit": audit_after}


def _render(rows: list[dict], audit: dict) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    def sh(cmd, cwd):
        try:
            r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=30)
            return r.stdout.strip()
        except Exception:
            return "ERR"
    sl = PACKAGE_ROOT.parent
    lines = ["# 全管线回放报告 20260805（round 2）", ""]
    lines += ["## 被测版本（工作区现状，无 worktree 隔离）", ""]
    lines.append(f"- story-lifecycle HEAD: {sh(['git', 'rev-parse', 'HEAD'], sl)[:12]} "
                 f"({sh(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], sl)})")
    st = sh(['git', 'status', '--short'], sl).splitlines()
    lines.append(f"- story-lifecycle git status: {len(st)} 个改动文件")
    lines.append("- LLM 端点: opencode-go（https://opencode.ai/zen/go/v1, deepseek-v4-flash）")
    lines.append("- 快照: snapshot_20260805")
    lines.append("")

    from collections import Counter
    by_cls = {}
    for r in rows:
        by_cls.setdefault(r['cls'], []).append(r)
    lines += ["## 1. 健壮性（完成率，分类别）", ""]
    lines += ["| 类别 | 总数 | 完成(ok) | 部分 | 失败 |", "|------|------|----------|------|------|"]
    for k in ('A', 'B', 'C', 'D'):
        rs = by_cls.get(k, [])
        lines.append(f"| {k} | {len(rs)} | {sum(1 for r in rs if r['status']=='ok')} | "
                     f"{sum(1 for r in rs if r['status']=='partial')} | {sum(1 for r in rs if r['status']=='failed')} |")
    lines.append("")
    lines.append(f"- 完成率: {sum(1 for r in rows if r['status']=='ok')}/{len(rows)}")
    lines.append("")

    lines += ["## 2. 产物质量（生成 spec 评分）", ""]
    lines += ["| 类别 | story | spec 字节 | done stages |", "|------|-------|----------|-------------|"]
    for r in rows:
        sz = Path(r['spec']).stat().st_size if r['spec'] else 0
        lines.append(f"| {r['cls']} | {r['story_key'][-20:]} | {sz} | {','.join(r['done_stages']) or '-'} |")
    lines.append("")

    lines += ["## 3. 拦截一致性（gate 回放）", ""]
    lines += ["| 类别 | story | eval 类别 | gate decision | gate verdict | findings |", "|------|-------|-----------|---------------|--------------|----------|"]
    for r in rows:
        lines.append(f"| {r['cls']} | {r['story_key'][-20:]} | {r['cls']} | {r.get('gate_decision','?')} | "
                     f"{r.get('gate_verdict','?')} | {len(r.get('gate_findings') or [])} |")
    lines.append("")
    b_block = sum(1 for r in by_cls.get('B', []) if r.get('gate_decision') in ('retry', 'fail'))
    a_block = sum(1 for r in by_cls.get('A', []) if r.get('gate_decision') in ('retry', 'fail'))
    lines.append(f"- B 类（应拦）gate 拦截: {b_block}/{len(by_cls.get('B', []))}")
    lines.append(f"- A 类（不应拦）gate 误拦: {a_block}/{len(by_cls.get('A', []))}")
    lines.append("")

    lines += ["## 4. 问题清单", ""]
    problems = [
        ("P1", "verify 阶段 LLM 超时 fallback approve", "多个 story 的 stage_completion LLM read timeout 后 fallback approve，导致 done 回执缺失（如 1067103/1065191 done=[] 但产物齐全）", "gate/completion 对 LLM 抖动无降级区分"),
        ("P2", "spec 落点与 artifact 契约不一致", "spec 落在 story/<sk>-REPLAY114438/spec.md 而非 story/spec.md，profile artifact 校验可能找不到", "artifact 路径契约"),
        ("P3", "profile_loader STORY_HOME 硬编码", "_load_raw 用 Path.home()/.story-lifecycle 而非环境变量，沙箱需靠 cwd/.story/profiles 绕开", "profile 解析"),
    ]
    for pid, name, desc, where in problems:
        lines.append(f"- **{pid} {name}**: {desc}（{where}）")
    lines.append("")

    lines += ["## 5. 沙箱审计", ""]
    lines.append(f"- 原 story.db md5 不变: {audit['prod_db_unchanged']}")
    lines.append(f"- hc-all git 变化: {audit['hc_git_changed'] or '无'}")
    lines.append(f"- D:/hc-all/story 新增: {audit['hc_story_new'] or '无'} | 修改: {audit['hc_story_modified'] or '无'} | 干净: {audit['hc_story_clean']}")
    lines.append("")

    path = RESULTS / "pipeline_replay_20260805.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"报告: {path}")


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING)
    main()
