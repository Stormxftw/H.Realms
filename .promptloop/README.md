# Game Host Console v1 Swarm Loop

This scaffold turns the approved v1 plan into a bounded, auditable agent loop:

`Observe → Normalize → Contract → Isolated lane → RED/GREEN/REFACTOR → Verify → Review → Integrate → Repeat`

## Authority

- Product scope: `docs/plans/2026-07-29-v1-release-plan.md`
- Wave order: `.promptloop/v1-swarm.yaml`
- Every coding lane uses its own Git worktree and branch.
- The orchestrator verifies every lane; agent self-reports are not evidence.

## Gates

1. **Pre-flight:** baseline snapshot exists, baseline tests pass, lane worktree is isolated, and its owned files are explicit.
2. **Revision:** spec review then quality/safety review; maximum three correction passes.
3. **Escalation:** merge conflict, ambiguous safety behavior, real-server mutation need, or revision-loop exhaustion pauses for Joe.
4. **Abort:** attempted out-of-scope/destructive action, repository corruption, missing snapshot, or unbounded data deletion.

## Hard prohibitions

- Do not stop, restart, restore, or otherwise mutate a real game server for routine verification.
- Do not deploy, push, install dependencies, or alter the live Hermes plugin without an explicit integration gate.
- Do not let two coding agents edit the same worktree.
- Do not commit files outside the lane contract.

## Evidence runner

```bash
.promptloop/run.sh <run-id> "<verification command>"
```

It writes ignored evidence under `.promptloop/runs/<run-id>/`:

- `events.log`
- `verification.log`
- `report.md`

A zero exit code means only that the supplied verification command passed; the orchestrator must still inspect the diff and close both review gates.
