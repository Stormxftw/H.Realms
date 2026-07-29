#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
BACKEND_TARGET="$HERMES_HOME/plugins/game-host-console"
DESKTOP_TARGET="$HERMES_HOME/desktop-plugins/game-host-console"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="$ROOT/.uninstall-backups/$STAMP"

mkdir -p "$BACKUP"
if [[ -d "$BACKEND_TARGET" ]]; then
  cp -a "$BACKEND_TARGET" "$BACKUP/backend"
fi
if [[ -d "$DESKTOP_TARGET" ]]; then
  cp -a "$DESKTOP_TARGET" "$BACKUP/desktop"
fi

hermes plugins disable game-host-console 2>/dev/null || true
rm -rf "$BACKEND_TARGET" "$DESKTOP_TARGET"

if [[ "${1:-}" == "--stop-service" ]]; then
  "$ROOT/stop.sh"
fi

echo "Removed the live Hermes Game Host Console plugins."
echo "Project source and control audit were preserved."
echo "Uninstall backup: $BACKUP"
echo "Restart the Hermes gateway and reload desktop plugins to clear the UI entry."
