"""可选加分项：为 hc-all 17 个子仓安装 commit-msg hook。

Hook 作用：从当前分支名提取 story_key（7 位以上数字段），如果 commit message
中还没出现，则在 message 末尾追加 `[story:<id>]`。这样即使 squash 提交也能保留
story 硬关联。

用法：
    cd packages/story-miner && python scripts/install_commit_msg_hooks.py
"""
from __future__ import annotations

import argparse
import os
import stat
from pathlib import Path

REPOS = [
    "hc-order", "hc-user", "hc-risk-management", "hc-message", "hc-config",
    "hc-limit", "hc-third-party", "hc-coupon", "hc-marketing", "hc-callback",
    "hc-gateway", "hc-job", "hc-audit", "hc-aiops", "hc-pytest",
    "story-board", "ys-frame-parent",
]

HOOK = r'''#!/bin/sh
# Auto-inject story_key from branch name into commit message.
# Installed by story-miner/scripts/install_commit_msg_hooks.py

MSG_FILE="$1"
BRANCH=$(git symbolic-ref --short HEAD 2>/dev/null || true)

if [ -z "$BRANCH" ]; then
    exit 0
fi

# Extract the first 7+ digit segment as story_key
STORY_KEY=$(echo "$BRANCH" | grep -oE '[0-9]{7,}' | head -n 1)

if [ -z "$STORY_KEY" ]; then
    exit 0
fi

# Skip merge commits (they already carry branch name in default message)
if grep -qE "^Merge branch '.*' into" "$MSG_FILE"; then
    exit 0
fi

if ! grep -qF "$STORY_KEY" "$MSG_FILE"; then
    printf "\n[story:%s]\n" "$STORY_KEY" >> "$MSG_FILE"
fi

exit 0
'''


def install(base: Path, dry_run: bool = False) -> dict[str, bool]:
    results: dict[str, bool] = {}
    for repo_name in REPOS:
        repo_path = base / repo_name
        git_dir = repo_path / ".git"
        if not git_dir.is_dir():
            results[repo_name] = False
            continue

        hooks_dir = git_dir / "hooks"
        hook_path = hooks_dir / "commit-msg"

        if dry_run:
            print(f"[dry-run] would write {hook_path}")
            results[repo_name] = True
            continue

        hooks_dir.mkdir(parents=True, exist_ok=True)
        hook_path.write_text(HOOK, encoding="utf-8")
        # chmod +x
        current = hook_path.stat().st_mode
        hook_path.chmod(current | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        print(f"[installed] {hook_path}")
        results[repo_name] = True

    return results


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="D:/hc-all", type=Path)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    results = install(args.base, args.dry_run)
    ok = sum(1 for v in results.values() if v)
    print(f"\n{ok}/{len(REPOS)} repos hooked")


if __name__ == "__main__":
    main()
