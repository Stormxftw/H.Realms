#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SESSION="${DASHBOARD_SESSION:-game-host-dashboard}"
PORT="${DASHBOARD_PORT:-5057}"
HOST="${DASHBOARD_HOST:-127.0.0.1}"
PID_FILE="$ROOT/dashboard.pid"
LOG="$ROOT/logs/dashboard.log"
cd "$ROOT"
mkdir -p logs data

LOCAL_ADAPTER_CONFIG="$ROOT/data/local-game-adapters.json"
if [[ -z "${GAME_HOST_ADAPTER_CONFIG:-}" && -f "$LOCAL_ADAPTER_CONFIG" ]]; then
  export GAME_HOST_ADAPTER_CONFIG="$LOCAL_ADAPTER_CONFIG"
fi

if curl -fsS --max-time 3 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
  echo "Hermes Game Host Console is already healthy on http://127.0.0.1:$PORT"
  ./status.sh || true
  exit 0
fi

python3 -m py_compile app.py control_engine.py
if command -v tmux >/dev/null 2>&1; then
  tmux kill-session -t "$SESSION" 2>/dev/null || true
  tmux new-session -d -s "$SESSION" -c "$ROOT" "DASHBOARD_PORT=$PORT DASHBOARD_HOST=$HOST exec python3 app.py --host '$HOST' --port '$PORT'"
  tmux pipe-pane -o -t "$SESSION" "cat >> '$LOG'"
  rm -f "$PID_FILE"
else
  rm -f "$PID_FILE"
  nohup env DASHBOARD_PORT="$PORT" DASHBOARD_HOST="$HOST" python3 app.py --host "$HOST" --port "$PORT" >> "$LOG" 2>&1 &
  echo $! > "$PID_FILE"
  disown || true
fi

for _ in $(seq 1 30); do
  if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    echo "Started Hermes Game Host Console on http://127.0.0.1:$PORT"
    echo "The authenticated Hermes plugin proxies this loopback-only service."
    exit 0
  fi
  sleep 0.25
done

echo "Dashboard did not become healthy. Recent log:" >&2
tail -40 "$LOG" >&2 || true
exit 1
