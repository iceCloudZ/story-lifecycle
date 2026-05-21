#!/bin/bash
# Quick test of story-lifecycle in WSL2
set -e

echo "=== Install tmux/ttyd ==="
sudo apt update -qq && sudo apt install -y -qq tmux ttyd 2>/dev/null

echo ""
echo "=== Setup project ==="
cd /mnt/d/story-lifecycle
python3 -m venv .venv 2>/dev/null || true
source .venv/bin/activate
pip install -e ".[dev]" -q

echo ""
echo "=== Clean old data ==="
pkill -f "story serve" 2>/dev/null || true
tmux kill-session -t s-STORY-TEST 2>/dev/null || true
rm -f ~/.story-lifecycle/story.db

echo ""
echo "=== Start server ==="
nohup story serve --host 127.0.0.1 --port 8180 > /tmp/story-server.log 2>&1 &
sleep 3
curl -s http://127.0.0.1:8180/api/session/health
echo ""

echo ""
echo "=== Create test story ==="
WS=$(pwd)
curl -s -X POST http://127.0.0.1:8180/api/story \
  -H 'Content-Type: application/json' \
  -d "{\"key\":\"STORY-TEST\",\"title\":\"Test Feature\",\"profile\":\"minimal\",\"workspace\":\"$WS\"}"

echo ""
echo "=== Check tmux ==="
sleep 8
tmux list-sessions 2>/dev/null || echo "(no tmux sessions)"
echo ""
echo "=== CC output ==="
tmux capture-pane -t s-STORY-TEST -p -S -20 2>/dev/null || echo "(session not ready)"

echo ""
echo "=== Done ==="
echo "Server log: cat /tmp/story-server.log"
echo "Attach: tmux attach -t s-STORY-TEST"
