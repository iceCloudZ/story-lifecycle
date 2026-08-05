"""沙箱三件套自证（round2 前置）：STORY_HOME 隔离 / AGENTS.md 截断爬升 / git 零副作用。

用法: python -m eval.sandbox_selfcheck
产出: packages/eval/sandbox/selfcheck_report.json
"""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent
SANDBOX = PACKAGE_ROOT / "sandbox"
STORY_HOME = SANDBOX / "story_home"
WS = SANDBOX / "ws" / "selfcheck"
SL_SRC = PACKAGE_ROOT.parent / "packages" / "story-lifecycle" / "src"

PROD_DB = Path.home() / ".story-lifecycle" / "story.db"


def md5(p: Path) -> str:
    if not p.exists():
        return "MISSING"
    return hashlib.md5(p.read_bytes()).hexdigest()


def git_state(repo: Path) -> str:
    r = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True,
                       text=True, timeout=30, encoding="utf-8")
    return r.stdout


def main() -> dict:
    report = {}
    prod_before = md5(PROD_DB)
    hc_all = Path("D:/hc-all")
    repos = [d for d in hc_all.iterdir() if d.is_dir() and (d / ".git").exists()]
    repos.append(hc_all / "frontends" / "hc-admin")
    git_before = {r.name: git_state(r) for r in repos}

    # STORY_HOME 隔离（绝对路径）
    STORY_HOME.mkdir(parents=True, exist_ok=True)
    os.environ["STORY_HOME"] = str(STORY_HOME)
    os.environ["STORY_LLM_BASE_URL"] = "https://opencode.ai/zen/go/v1"
    sys.path.insert(0, str(SL_SRC))

    from story_lifecycle.infra.db import models as db

    sandbox_db = db.get_db_path()
    report["sandbox_db_path"] = str(sandbox_db)
    report["sandbox_db_isolated"] = str(sandbox_db).startswith(str(STORY_HOME))

    # 建表（沙箱内 init schema）
    try:
        db.init_db()
        report["schema_init_ok"] = True
    except Exception as e:
        report["schema_init_ok"] = False
        report["schema_init_err"] = str(e)[:200]

    try:
        db.upsert_story("selfcheck-story", title="selfcheck", workspace=str(WS),
                        profile="minimal", current_stage="design", status="active")
        report["db_write_ok"] = True
    except Exception as e:
        report["db_write_ok"] = False
        report["db_write_err"] = str(e)[:200]

    # AGENTS.md 截断证据爬升
    WS.mkdir(parents=True, exist_ok=True)
    (WS / "AGENTS.md").write_text("# selfcheck sandbox workspace\n", encoding="utf-8")
    from story_lifecycle.infra.story_paths import story_evidence_root

    ev = story_evidence_root(WS)
    report["evidence_root"] = str(ev)
    report["evidence_in_sandbox"] = str(ev).startswith(str(SANDBOX))
    report["evidence_truncated_at_ws"] = str(ev) == str(WS / "story")

    # 无副作用复检
    prod_after = md5(PROD_DB)
    report["prod_db_md5_before"] = prod_before
    report["prod_db_md5_after"] = prod_after
    report["prod_db_unchanged"] = prod_before == prod_after
    git_after = {r.name: git_state(r) for r in repos}
    report["hc_git_changed"] = [n for n in git_before if git_before[n] != git_after.get(n, "")]
    report["hc_git_clean"] = len(report["hc_git_changed"]) == 0

    # 清理
    try:
        db.execute("DELETE FROM story WHERE story_key = 'selfcheck-story'")
    except Exception:
        pass

    out = SANDBOX / "selfcheck_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    ok = (
        report["sandbox_db_isolated"] and report["schema_init_ok"] and report["db_write_ok"]
        and report["evidence_in_sandbox"] and report["evidence_truncated_at_ws"]
        and report["prod_db_unchanged"] and report["hc_git_clean"]
    )
    print("SELFCHECK:", "PASS" if ok else "FAIL")
    return report


if __name__ == "__main__":
    main()
