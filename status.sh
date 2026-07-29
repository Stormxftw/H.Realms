#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SESSION="${DASHBOARD_SESSION:-game-host-dashboard}"
PORT="${DASHBOARD_PORT:-5057}"
PID_FILE="$ROOT/dashboard.pid"

echo "=== Hermes Game Host Console ==="
if command -v tmux >/dev/null 2>&1 && tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "runtime: tmux session $SESSION"
elif [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "runtime: PID $(cat "$PID_FILE")"
else
  echo "runtime: not running"
fi

if curl -fsS --max-time 4 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
  echo "health:  ok"
  echo "local:   http://127.0.0.1:$PORT"
else
  echo "health:  unavailable"
fi

echo
echo "=== Listener ==="
ss -ltnp 2>/dev/null | grep -E ":${PORT}\\b" || echo "no TCP listener on port $PORT"

echo
echo "=== Recent log ==="
tail -20 "$ROOT/logs/dashboard.log" 2>/dev/null || echo "no log yet"
