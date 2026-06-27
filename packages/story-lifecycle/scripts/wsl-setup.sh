#!/bin/bash
# Story Lifecycle Manager — WSL2 setup & test
# Run this inside your WSL2 terminal

set -e

echo "=== 1. Check environment ==="
python3 --version
echo "tmux: $(which tmux 2>/dev/null || echo 'NOT FOUND — install: sudo apt install tmux')"
echo "ttyd: $(which ttyd 2>/dev/null || echo 'NOT FOUND — install: sudo apt install ttyd')"
echo "git:  $(which git)"

echo ""
echo "=== 2. Install tmux/ttyd if missing ==="
if ! command -v tmux &>/dev/null; then
    sudo apt update && sudo apt install -y tmux
fi
if ! command -v ttyd &>/dev/null; then
    sudo apt update && sudo apt install -y ttyd
fi

echo ""
echo "=== 3. Clone & setup project ==="
cd ~
if [ ! -d story-lifecycle ]; then
    git clone https://github.com/iceCloudZ/story-lifecycle.git
fi
cd story-lifecycle
git pull --rebase

python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

echo ""
echo "=== 4. Start orchestrator ==="
pkill -f "story serve" 2>/dev/null || true
nohup story serve --host 127.0.0.1 --port 8180 > /tmp/story-server.log 2>&1 &
sleep 3
curl -s http://127.0.0.1:8180/api/session/health

echo ""
echo "=== 5. Create test story ==="
story new STORY-1065520 --title "职业邮箱限制" --profile minimal --workspace ~

echo ""
echo "=== 6. Check tmux session ==="
sleep 5
tmux list-sessions 2>/dev/null || echo "(no tmux sessions)"
tmux capture-pane -t s-STORY-1065520 -p -S -20 2>/dev/null || echo "(session not ready yet)"

echo ""
echo "=== Done! ==="
echo "Dashboard: story board"
echo "Terminal:  story enter STORY-1065520"
echo "Or attach: tmux attach -t s-STORY-1065520"
