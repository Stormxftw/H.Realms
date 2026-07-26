#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SESSION="${DASHBOARD_SESSION:-game-host-dashboard}"
PORT="${DASHBOARD_PORT:-5057}"
HOST="${DASHBOARD_HOST:-0.0.0.0}"
cd "$ROOT"; mkdir -p logs
if tmux has-session -t "$SESSION" 2>/dev/null; then echo "Dashboard already running in tmux session '$SESSION'."; ./status.sh || true; exit 0; fi
python3 -m py_compile app.py
tmux new-session -d -s "$SESSION" -c "$ROOT" "DASHBOARD_PORT=$PORT DASHBOARD_HOST=$HOST exec python3 app.py --host '$HOST' --port '$PORT'"
tmux pipe-pane -o -t "$SESSION" "cat >> '$ROOT/logs/dashboard.log'"
echo "Started Hermes Game Host Console on http://$HOST:$PORT"
echo "Local: http://127.0.0.1:$PORT"
echo "LAN if portproxy is active: http://203.0.113.10:$PORT"
