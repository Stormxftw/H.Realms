#!/usr/bin/env bash
set -uo pipefail

if [[ $# -lt 2 ]]; then
  echo "usage: $0 <run-id> <verification-command>" >&2
  exit 64
fi

run_id="$1"
shift
verification_command="$*"

if [[ ! "$run_id" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "invalid run id" >&2
  exit 64
fi

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
run_dir="$root/.promptloop/runs/$run_id"
if [[ -e "$run_dir" ]]; then
  echo "run already exists: $run_dir" >&2
  exit 73
fi
mkdir -p "$run_dir"

started="$(date -Iseconds)"
printf '%s start run=%s\n' "$started" "$run_id" > "$run_dir/events.log"
printf 'command=%q\n' "$verification_command" >> "$run_dir/events.log"

set +e
(
  cd "$root"
  bash -lc "$verification_command"
) > >(tee "$run_dir/verification.log") 2>&1
status=$?
set -e

finished="$(date -Iseconds)"
printf '%s finish status=%s\n' "$finished" "$status" >> "$run_dir/events.log"

cat > "$run_dir/report.md" <<EOF
# PromptLoop verification report

- **Run:** $run_id
- **Started:** $started
- **Finished:** $finished
- **Exit code:** $status
- **Verdict:** $([[ $status -eq 0 ]] && echo PASS || echo FAIL)
- **Verification log:** \`verification.log\`

This verdict covers only the supplied verification command. Diff inspection and review gates remain separate.
EOF

printf 'report=%s status=%s\n' "$run_dir/report.md" "$status"
exit "$status"
