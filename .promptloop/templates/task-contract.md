# Task Contract

- **Story / objective:**
- **Authoritative acceptance criteria:**
- **Repository / worktree:**
- **Branch:**
- **Owned files:**
- **Allowed actions:** edit owned files; run tests against disposable fixtures; create a lane-local commit.
- **Forbidden actions:** real game lifecycle/restore; live deployment; dependency installation; push; editing another lane; unrelated cleanup.
- **TDD proof:** record the failing test and expected failure before production edits, then targeted green and full regression commands.
- **Verification commands:**
- **Deliverables:** commit SHA, exact changed files, RED/GREEN evidence, test results, remaining risks.
- **Approval gates:** spec PASS, quality/safety APPROVED, orchestrator integration PASS.
- **Final response:** `COMPLETE`, `BLOCKED`, or `NEEDS_HUMAN`, followed by Claim → Evidence entries.
