#!/usr/bin/env bash
set -Eeuo pipefail
SESSION="${DASHBOARD_SESSION:-game-host-dashboard}"
if tmux has-session -t "$SESSION" 2>/dev/null; then tmux kill-session -t "$SESSION"; echo "Stopped dashboard session '$SESSION'."; else echo "Dashboard is not running."; fi
