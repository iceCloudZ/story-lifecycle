#!/usr/bin/env bash
# Refresh miner derived artifacts.
# Usage:
#   refresh.sh           # daily incremental: ingest last 1 day, refresh playbooks
#   refresh.sh full      # weekly full: ingest all, refresh playbooks + link + failure knowledge
#
# Hermes cron can call this script directly.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

MODE="${1:-incremental}"
PYTHON="${PYTHON:-python}"

echo "=== miner refresh started: mode=$MODE ==="
echo "cwd: $(pwd)"
echo "python: $PYTHON"

if [[ "$MODE" == "full" ]]; then
    echo "[1/10] full ingest ..."
    "$PYTHON" -m miner.store

    echo "[2/10] ingest stories from .story/ ..."
    "$PYTHON" -m miner.story_ingest

    echo "[3/10] link sessions to stories ..."
    "$PYTHON" -m miner.link

    echo "[4/10] generate playbooks ..."
    "$PYTHON" scripts/generate_playbooks.py

    echo "[5/10] analyze failure modes ..."
    "$PYTHON" scripts/failure_mode.py

    # 结果轴 outcome mining（full 才跑——TAPD/git/LLM，慢；每步容错，单失败不中断）
    echo "[6/10] bug↔story (TAPD reverse) ..."
    "$PYTHON" scripts/bug_story_graph.py --no-detail || echo "  (bug_story_graph failed, skip)"

    echo "[7/10] task_type classify (LLM) ..."
    "$PYTHON" scripts/classify_story_task_type.py || echo "  (classify failed, skip)"

    echo "[8/10] story→commit (branch) ..."
    "$PYTHON" scripts/story_commits.py || echo "  (story_commits failed, skip)"

    echo "[9/10] infer branchless commits (--all-branchless, slow) ..."
    "$PYTHON" scripts/infer_bug_magnet_commits.py --all-branchless || echo "  (infer failed, skip)"

    echo "[10/10] result-axis phase2 (bug-prone/cycle-time/churn) ..."
    "$PYTHON" scripts/result_axis_phase2.py || echo "  (result_axis_phase2 failed, skip)"
else
    echo "[1/4] incremental ingest (since 1 day) ..."
    "$PYTHON" -m miner.store --since-days 1

    echo "[2/4] ingest stories from .story/ ..."
    "$PYTHON" -m miner.story_ingest

    echo "[3/4] link sessions to stories ..."
    "$PYTHON" -m miner.link

    echo "[4/4] generate playbooks ..."
    "$PYTHON" scripts/generate_playbooks.py
fi

echo "=== miner refresh finished: mode=$MODE ==="
