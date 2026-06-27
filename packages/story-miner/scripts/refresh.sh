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
    echo "[1/5] full ingest ..."
    "$PYTHON" -m miner.store

    echo "[2/5] ingest stories from .story/ ..."
    "$PYTHON" -m miner.story_ingest

    echo "[3/5] link sessions to stories ..."
    "$PYTHON" -m miner.link

    echo "[4/5] generate playbooks ..."
    "$PYTHON" scripts/generate_playbooks.py

    echo "[5/5] analyze failure modes ..."
    "$PYTHON" scripts/failure_mode.py
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
