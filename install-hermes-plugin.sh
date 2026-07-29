#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
BACKEND_TARGET="$HERMES_HOME/plugins/game-host-console/dashboard"
DESKTOP_TARGET="$HERMES_HOME/desktop-plugins/game-host-console"
BACKUP_ROOT="$ROOT/.install-backups"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="$BACKUP_ROOT/$STAMP"

mkdir -p "$BACKUP"
if [[ -d "$BACKEND_TARGET" ]]; then
  mkdir -p "$BACKUP/backend"
  cp -a "$BACKEND_TARGET/." "$BACKUP/backend/"
fi
if [[ -d "$DESKTOP_TARGET" ]]; then
  mkdir -p "$BACKUP/desktop"
  cp -a "$DESKTOP_TARGET/." "$BACKUP/desktop/"
fi

mkdir -p "$BACKEND_TARGET/dist" "$BACKEND_TARGET/web" "$DESKTOP_TARGET"
cp "$ROOT/hermes-plugin/plugin.yaml" "$(dirname "$BACKEND_TARGET")/plugin.yaml"
cp "$ROOT/hermes-plugin/dashboard/manifest.json" "$BACKEND_TARGET/manifest.json"
cp "$ROOT/hermes-plugin/dashboard/plugin_api.py" "$BACKEND_TARGET/plugin_api.py"
cp "$ROOT/hermes-plugin/dashboard/dist/index.js" "$BACKEND_TARGET/dist/index.js"
cp "$ROOT/static/index.html" "$BACKEND_TARGET/web/index.html"
cp "$ROOT/static/app.css" "$BACKEND_TARGET/web/app.css"
cp "$ROOT/static/app.js" "$BACKEND_TARGET/web/app.js"
cp "$ROOT/desktop-plugin/plugin.js" "$DESKTOP_TARGET/plugin.js"

hermes plugins enable game-host-console --no-allow-tool-override
"$ROOT/start.sh"

echo "Installed Game Host Console bridge."
echo "Backend: $BACKEND_TARGET"
echo "Desktop: $DESKTOP_TARGET"
echo "Backup:  $BACKUP"
echo "Restart the Hermes gateway, then run 'Reload desktop plugins' from the Desktop command palette."
