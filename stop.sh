#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SESSION="${DASHBOARD_SESSION:-game-host-dashboard}"
PID_FILE="$ROOT/dashboard.pid"

if command -v tmux >/dev/null 2>&1 && tmux has-session -t "$SESSION" 2>/dev/null; then
  tmux kill-session -t "$SESSION"
  echo "Stopped Hermes Game Host Console tmux session."
elif [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  PID="$(cat "$PID_FILE")"
  kill "$PID"
  for _ in $(seq 1 30); do
    kill -0 "$PID" 2>/dev/null || break
    sleep 0.2
  done
  kill -9 "$PID" 2>/dev/null || true
  echo "Stopped Hermes Game Host Console PID $PID."
else
  echo "Hermes Game Host Console is not running."
fi
rm -f "$PID_FILE"
