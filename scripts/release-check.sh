#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
NODE_BIN="${NODE_BIN:-node}"
HERMES_PYTHON="${HERMES_PYTHON:-${HOME:?HOME must be set when HERMES_PYTHON is not overridden}/.hermes/hermes-agent/venv/bin/python}"
TSC_BIN="${TSC_BIN:-${HOME:?HOME must be set when TSC_BIN is not overridden}/.hermes/hermes-agent/node_modules/.bin/tsc}"
GIT_BIN="${GIT_BIN:-git}"
TAR_BIN="${TAR_BIN:-tar}"
RELEASE_TEMP_DIR=""

cleanup_release_temp() {
  local exit_status=$?
  if [[ -n "$RELEASE_TEMP_DIR" ]] && ! rm -rf -- "$RELEASE_TEMP_DIR"; then
    printf 'warning: could not remove release-check temp directory: %s\n' "$RELEASE_TEMP_DIR" >&2
  fi
  return "$exit_status"
}

trap cleanup_release_temp EXIT

REQUIRED_RELEASE_ARTIFACTS=(
  pyproject.toml
  app.py
  control_engine.py
  registry.py
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
  tests/test_registry.py
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

REQUIRED_EXECUTABLE_ARTIFACTS=(
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

check_clean_source_tree() {
  local status
  status="$("$GIT_BIN" status --porcelain=v1 --untracked-files=all --ignored=no)"
  if [[ -n "$status" ]]; then
    printf 'release source tree must be clean; commit or remove these source changes:\n%s\n' "$status" >&2
    return 1
  fi
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
  local archive_path archive_list archive_modes artifact line mode
  RELEASE_TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/game-host-release-check.XXXXXX")"
  archive_path="$RELEASE_TEMP_DIR/release.tar"
  archive_list="$RELEASE_TEMP_DIR/release-files.txt"
  archive_modes="$RELEASE_TEMP_DIR/release-modes.txt"

  "$GIT_BIN" archive --format=tar --output="$archive_path" HEAD
  "$TAR_BIN" -tf "$archive_path" > "$archive_list"
  "$TAR_BIN" -tvf "$archive_path" > "$archive_modes"

  declare -A archived=()
  while IFS= read -r artifact; do
    archived["$artifact"]=1
  done < "$archive_list"

  declare -A archived_modes=()
  while IFS= read -r line; do
    mode="${line%% *}"
    artifact="${line##* }"
    archived_modes["$artifact"]="$mode"
  done < "$archive_modes"

  for artifact in "${REQUIRED_RELEASE_ARTIFACTS[@]}"; do
    if [[ -z "${archived["$artifact"]+present}" ]]; then
      printf 'missing required tracked release artifact: %s\n' "$artifact" >&2
      return 1
    fi
  done

  for artifact in "${REQUIRED_EXECUTABLE_ARTIFACTS[@]}"; do
    if [[ "${archived_modes["$artifact"]:-}" != *x* ]]; then
      printf 'required release executable is not executable in git archive: %s\n' "$artifact" >&2
      return 1
    fi
  done

  rm -rf -- "$RELEASE_TEMP_DIR"
  RELEASE_TEMP_DIR=""
  printf '%s required tracked artifacts present in git archive HEAD\n' "${#REQUIRED_RELEASE_ARTIFACTS[@]}"
}

cd "$ROOT"
check_clean_source_tree
run_step "Python tests" "$PYTHON_BIN" -m unittest discover -s tests -v
run_step "Standalone UI tests" "$NODE_BIN" tests/ui.test.js
run_step "Desktop plugin contract" "$NODE_BIN" tests/desktop_plugin.test.js
run_step "Hermes-venv bridge tests" "$HERMES_PYTHON" -m unittest tests.test_plugin_api -v
run_step "JSON Schema validation" check_schemas
run_step "TypeScript noEmit" "$TSC_BIN" --project tests/tsconfig.desktop-plugin.json --noEmit --pretty false
run_step "Whitespace errors" "$GIT_BIN" diff --check
run_step "Release source tree unchanged" check_clean_source_tree
run_step "Tracked release archive" check_release_archive

printf '\nRelease checks passed.\n'
