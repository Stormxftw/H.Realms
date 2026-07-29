#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
NODE_BIN="${NODE_BIN:-node}"
HERMES_PYTHON="${HERMES_PYTHON:-/home/zim/.hermes/hermes-agent/venv/bin/python}"
TSC_BIN="${TSC_BIN:-/home/zim/.hermes/hermes-agent/node_modules/.bin/tsc}"
GIT_BIN="${GIT_BIN:-git}"
TAR_BIN="${TAR_BIN:-tar}"

REQUIRED_RELEASE_ARTIFACTS=(
  pyproject.toml
  app.py
  control_engine.py
  game_adapters.json
  static/index.html
  static/app.css
  static/app.js
  desktop-plugin/plugin.js
  hermes-plugin/plugin.yaml
  hermes-plugin/dashboard/manifest.json
  hermes-plugin/dashboard/plugin_api.py
  hermes-plugin/dashboard/dist/index.js
  schemas/game-adapter-config.schema.json
  schemas/game-control-profile.schema.json
  game_profiles/_template.json
  game_profiles/cs2.json
  game_profiles/dont-starve-together.json
  game_profiles/enshrouded.json
  game_profiles/minecraft.json
  game_profiles/palworld.json
  game_profiles/satisfactory.json
  game_profiles/sons-of-the-forest.json
  game_profiles/terraria.json
  game_profiles/valheim.json
  tests/test_app_api.py
  tests/test_control_engine.py
  tests/test_plugin_api.py
  tests/test_telemetry.py
  tests/ui.test.js
  tests/desktop_plugin.test.js
  tests/tsconfig.desktop-plugin.json
  tests/test_release_scaffold.py
  scripts/release-check.sh
  start.sh
  status.sh
  stop.sh
  install-hermes-plugin.sh
  uninstall-hermes-plugin.sh
)

run_step() {
  local label="$1"
  shift
  printf '\n==> %s\n' "$label"
  "$@"
}

check_schemas() {
  "$PYTHON_BIN" - "$ROOT" <<'PY'
import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

root = Path(sys.argv[1])


def load_json(path: Path) -> object:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


profile_schema_path = root / "schemas" / "game-control-profile.schema.json"
adapter_schema_path = root / "schemas" / "game-adapter-config.schema.json"
profile_schema = load_json(profile_schema_path)
adapter_schema = load_json(adapter_schema_path)

Draft202012Validator.check_schema(profile_schema)
Draft202012Validator.check_schema(adapter_schema)
profile_validator = Draft202012Validator(profile_schema)
adapter_validator = Draft202012Validator(adapter_schema)

profile_paths = sorted((root / "game_profiles").glob("*.json"))
if not profile_paths:
    raise SystemExit("no game profiles found for schema validation")
for profile_path in profile_paths:
    profile_validator.validate(load_json(profile_path))

adapter_validator.validate(load_json(root / "game_adapters.json"))
print(f"validated {len(profile_paths)} profiles and the adapter registry")
PY
}

check_release_archive() {
  local archive_dir archive_path archive_list artifact
  archive_dir="$(mktemp -d "${TMPDIR:-/tmp}/game-host-release-check.XXXXXX")"
  archive_path="$archive_dir/release.tar"
  archive_list="$archive_dir/release-files.txt"

  "$GIT_BIN" archive --format=tar --output="$archive_path" HEAD
  "$TAR_BIN" -tf "$archive_path" > "$archive_list"

  declare -A archived=()
  while IFS= read -r artifact; do
    archived["$artifact"]=1
  done < "$archive_list"

  for artifact in "${REQUIRED_RELEASE_ARTIFACTS[@]}"; do
    if [[ -z "${archived["$artifact"]+present}" ]]; then
      printf 'missing required tracked release artifact: %s\n' "$artifact" >&2
      rm -rf "$archive_dir"
      return 1
    fi
  done

  rm -rf "$archive_dir"
  printf '%s required tracked artifacts present in git archive HEAD\n' "${#REQUIRED_RELEASE_ARTIFACTS[@]}"
}

cd "$ROOT"
run_step "Python tests" "$PYTHON_BIN" -m unittest discover -s tests -v
run_step "Standalone UI tests" "$NODE_BIN" tests/ui.test.js
run_step "Desktop plugin contract" "$NODE_BIN" tests/desktop_plugin.test.js
run_step "Hermes-venv bridge tests" "$HERMES_PYTHON" -m unittest tests.test_plugin_api -v
run_step "JSON Schema validation" check_schemas
run_step "TypeScript noEmit" "$TSC_BIN" --project tests/tsconfig.desktop-plugin.json --noEmit --pretty false
run_step "Whitespace errors" "$GIT_BIN" diff --check
run_step "Tracked release archive" check_release_archive

printf '\nRelease checks passed.\n'
