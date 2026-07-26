#!/usr/bin/env bash
set -Eeuo pipefail
PORT="${DASHBOARD_PORT:-5057}"; SESSION="${DASHBOARD_SESSION:-game-host-dashboard}"; ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "=== Hermes Game Host Console ==="; if tmux has-session -t "$SESSION" 2>/dev/null; then echo "tmux: running ($SESSION)"; else echo "tmux: not running ($SESSION)"; fi
echo; echo "=== listener ==="; (ss -ltnp 2>/dev/null | grep -E ":$PORT\\b" || true)
echo; echo "=== health ==="; (curl -fsS --max-time 5 "http://127.0.0.1:$PORT/health" || true); echo
echo; echo "=== recent log ==="; tail -n 30 "$ROOT/logs/dashboard.log" 2>/dev/null || true
