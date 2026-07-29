# Hermes Game Host Console v1 Release Plan

> **For Hermes:** After Joe approves this plan, implement it with `subagent-driven-development` and strict TDD. Do not begin implementation from this document without that approval.

**Goal:** Turn the current working MVP into a trustworthy v1 for operating already-installed local game servers without lying about readiness, status, action outcomes, or recoverability.

**Architecture:** Keep declarative game profiles and approved adapter mappings, but put a validated registry and capability model in front of them. Execute lifecycle actions as durable per-game operations with backend-enforced preconditions, postcondition checks, audit events, and adapter-specific recovery. Store the operation database in the Linux-native XDG state directory rather than the NTFS/FUSE project mount. Keep the mutation engine loopback-only and reach it remotely only through the authenticated Hermes bridge.

**Tech stack:** Python standard library HTTP service, JSON Schema validation, SQLite for durable operation/audit state, Hermes Desktop Plugin SDK, shell adapters, Python `unittest`, Node contract tests.

---

## Review verdict

**BLOCKED for v1; sound MVP foundation.**

The current build is useful and the existing green path is real:

- Nine profiles render from declarative JSON.
- The service is healthy on loopback `127.0.0.1:5057`.
- The Hermes backend plugin is enabled at version `0.2.0`.
- Preview/confirm/apply, actor matching, one-use plans, per-game locks, property backup, and an append-only audit file exist.
- Current automated checks pass: 10 Python tests, the standalone UI test, the Desktop plugin contract test, and the FastAPI bridge test.
- The live UI is visually coherent and has no browser-console errors.

It is not yet a credible v1 because seven displayed games are not installed but still expose an enabled **Start server** action, status is partly inferred from inaccurate/hardcoded networking data, adapter schemas are documented but not enforced by the runtime, server actions trust script exit codes without verifying the resulting server state, failures are not durably visible, and recovery exists as files rather than an operator workflow.

### Confirmed blocker evidence

Read-only source inspection and isolated temporary-fixture probes confirmed:

| Finding | Evidence |
|---|---|
| Missing digest can mutate | `ControlEngine.apply(..., plan_digest=None)` accepted an isolated fake-runner action. |
| Stale profile plan can mutate | Editing the profile after planning did not invalidate an isolated fake-runner apply despite the stored `profileDigest`. |
| UI state rules are bypassable | An `enabledWhen: offline` control could be planned/applied without backend status enforcement. |
| Partial restart is not audited | In a fake restart where Stop succeeded and Start failed, apply raised and no audit file was created. |
| Adapter paths are not confined | An absolute adapter script path resolved to `/bin/true` and was accepted by the isolated fake runner. |
| Seven games are not installed | Only Minecraft and Palworld project directories and approved scripts exist on this host. |
| Runtime is not supervised | The live service is a Python process in a terminal scope; no matching user service unit exists. |
| Current HEAD is not a release artifact | Critical v1 files including `control_engine.py`, adapters, profiles, plugins, installers, and tests are still untracked and therefore absent from `git archive HEAD`. |
| Repository release check is not clean | `git diff --check` currently reports trailing whitespace in the in-progress `app.py` changes. |
| Authenticated bridge is live | Unauthenticated Hermes plugin requests returned `401`, and deployed plugin file hashes matched project source. |

No real game server lifecycle or restore action was executed during this review.

## Product boundary for v1

### Fully managed in v1

- **Minecraft Java**
- **Palworld**

A game is called *fully managed* only when its install readiness, process identity, lifecycle scripts, endpoint probes, logs, backup creation, and tested restore path are all configured and verified.

### Catalog-only templates in v1

- Valheim
- Counter-Strike 2
- Terraria
- Don't Starve Together
- Satisfactory
- Enshrouded
- Sons of the Forest

These may remain visible as setup templates, but the console must label them **Not installed** or **Needs setup**, explain the blockers, and disable mutation controls. An offline server and a nonexistent server installation are not the same state.

### Explicitly deferred beyond v1

- One-click SteamCMD download/update for every game
- Automatic token acquisition, firewall changes, or router port forwarding
- RCON chat/player administration
- Historical metrics dashboards and alerts
- Multi-host clustering
- Multi-user RBAC
- Modpack/mod management
- Direct unauthenticated LAN or internet control
- Agent-generated executable server scripts

The small spellbook wins here. A launcher that safely operates two real servers is v1; a launcher that pretends to operate nine is theater.

---

## v1 user stories

### GHC-001 — Validated, confined game registry (P0)

**Story:** As a server operator, I want the console to reject malformed or unsafe game definitions before it starts, so a bad profile or adapter cannot create misleading controls or execute outside the intended game directory.

**Why this is required:** `schemas/*.json` exist, but `control_engine.py` currently parses adapter/profile JSON without validating it against those schemas. `projectDir` and script paths are joined directly and are not proven to remain under `PROJECTS_ROOT`.

**Acceptance criteria:**

- Runtime validates `game_adapters.json` and every non-template profile against the checked-in schemas during startup.
- Startup fails closed with exact file/field diagnostics for invalid JSON, unknown fields, unsupported versions, duplicate game/control IDs, missing adapter/profile pairs, unknown bindings, and invalid value metadata.
- Every resolved `projectDir` remains beneath `PROJECTS_ROOT`; every approved script remains beneath its resolved game directory.
- Approved scripts must be regular executable files, not symlinks, before a capability is marked ready.
- Profile controls may reference only actions declared by the matching adapter.
- The backend owns action risk and confirmation policy; a profile cannot downgrade a destructive adapter action to avoid stronger confirmation.
- Invalid registry state cannot produce a partially working mutation API.
- Tests cover traversal (`../`), absolute paths, symlinks, duplicate IDs, missing scripts, non-executable scripts, and schema drift.

**Likely files:**

- Modify: `control_engine.py`
- Modify: `schemas/game-adapter-config.schema.json`
- Modify: `schemas/game-control-profile.schema.json`
- Create: `registry.py`
- Test: `tests/test_registry.py`

---

### GHC-002 — Installation readiness and capability-aware controls (P0)

**Story:** As a server operator, I want each game to say whether it is ready, stopped, running, degraded, or not installed, so I never confuse an absent installation with an offline server.

**Why this is required:** Seven of nine registered project directories do not exist, yet their Start buttons are enabled. The UI currently collapses all those conditions into **Offline**.

**Acceptance criteria:**

- Each game exposes a readiness state: `ready`, `needs_setup`, `misconfigured`, or `unavailable`.
- Readiness includes structured blockers such as missing project directory, binary, config, token, world/save, executable script, port definition, or backup path.
- The API returns explicit capabilities per game (`canStart`, `canStop`, `canRestart`, `canConfigure`, `canBackup`, `canRestore`, `canViewLogs`) with reasons when false.
- Top-level status reports the actual control policy (`preview-confirm-audit`) and mutation availability; it does not claim `readOnly: true` while mutation endpoints are enabled.
- The backend rejects an unavailable capability even if a client bypasses the UI.
- The Desktop and standalone UIs show setup state and blocker text, and do not render a clickable mutation as if it could succeed.
- Catalog-only games remain useful as documented templates without claiming full support.

**Likely files:**

- Modify: `game_adapters.json`
- Modify: `app.py`
- Modify: `control_engine.py`
- Modify: `desktop-plugin/plugin.js`
- Modify: `static/app.js`
- Test: `tests/test_readiness.py`
- Test: `tests/ui.test.js`

---

### GHC-003 — Truthful, portable server status (P0)

**Story:** As a server operator, I want process, listener, protocol-query, and endpoint health reported separately, so **Online**, **Offline**, and **Degraded/Unknown** mean something trustworthy.

**Why this is required:** Minecraft currently uses ping success as its `online` truth even when process state may differ. Generic listener probing always uses UDP socket output, including TCP games. LAN/public addresses are hardcoded or externally discovered, and Steam games that query on `port + 1` or `port + 3` cannot represent that in the adapter.

**Acceptance criteria:**

- Status distinguishes at least: `running_ready`, `running_degraded`, `stopped`, `not_installed`, and `unknown`.
- Process detection, listener detection, and protocol query each report their own result and error; one failed probe does not silently become **Offline**.
- TCP games use TCP listener inspection and UDP games use UDP inspection.
- Adapters can declare a distinct `queryPort` and any additional required ports.
- Minecraft, Palworld, and Steam A2S collectors have deterministic parser/probe tests, including timeouts and malformed packets.
- LAN addresses are discovered from the host or explicitly configured; no hardcoded personal or public IP fallback remains in product code.
- Public-IP lookup is optional, clearly labeled, cached, and disabled without breaking local/LAN status.
- The UI exposes degraded/unknown explanations instead of a red dot with no diagnosis.

**Likely files:**

- Create: `telemetry.py`
- Modify: `app.py`
- Modify: `game_adapters.json`
- Modify: `schemas/game-adapter-config.schema.json`
- Modify: `desktop-plugin/plugin.js`
- Modify: `static/app.js`
- Test: `tests/test_telemetry.py`

---

### GHC-004 — Backend-enforced, verified lifecycle operations (P0)

**Story:** As a server operator, I want Start, Stop, and Restart to enforce live preconditions and verify their postconditions, so a green toast means the server actually reached the requested state.

**Why this is required:** `enabledWhen` is enforced only by JavaScript. A direct API call can plan a nonsensical action, and successful script exit is treated as successful server state without a readiness check.

**Acceptance criteria:**

- The backend enforces operation preconditions from fresh status, not cached UI state.
- The plan captures a status fingerprint and profile digest; apply requires `planDigest` and revalidates both before mutation.
- Plans expire after a short fixed TTL, are purged when applied/rejected/abandoned, and the in-memory plan set has a hard bound.
- Start on a running server and Stop on a stopped server return structured idempotent outcomes without launching duplicate processes.
- Only one lifecycle/config/backup operation may execute per game at a time; conflicts return `409` with the active operation ID.
- Start polls until ready or reports a bounded timeout; Stop polls until the managed process is gone; Restart proves both transitions.
- Process identity is adapter-specific and cannot kill a merely similar process.
- Script exit 0 with a failed postcondition is recorded and surfaced as failure.
- Failure attempts, timeouts, and partial restart failures are audited with truthful recovery guidance.
- Tests use disposable fake server processes and fault injection; live destructive actions are not part of routine tests.

**Likely files:**

- Modify: `control_engine.py`
- Create: `operations.py`
- Modify: `app.py`
- Test: `tests/test_lifecycle_operations.py`
- Test: `tests/test_control_engine.py`

---

### GHC-005 — Durable operation state and activity history (P0)

**Story:** As a server operator, I want long-running actions to have durable IDs and visible outcomes, so a browser timeout, refresh, or Hermes restart does not leave me wondering whether a server actually started, stopped, or backed up.

**Why this is required:** Apply currently holds an HTTP request for up to 320 seconds, consumes the plan before execution, and keeps UI activity only in browser memory. Audit records are written only after successful actions.

**Acceptance criteria:**

- Applying a plan creates an `operationId` and durable state: `queued`, `running`, `succeeded`, `failed`, `cancelled` where safe, or `outcome_unknown` after interrupted recovery.
- The API provides allow-listed operation list/detail endpoints and the UI polls active operations.
- A client refresh or reconnect resumes the same operation view rather than retrying the command.
- Per-operation records include requested action, actor/source, timestamps, bounded/redacted output, precondition, postcondition, and recovery note.
- Failed and rejected mutations are durably visible; secrets, tokens, passwords, and full private paths are not stored or displayed.
- Service restart recovery reconciles operations that were `running` with actual process/postcondition state.
- Durable state defaults to `${XDG_STATE_HOME:-~/.local/state}/hermes-game-host-console/operations.db`, with a `0700` state directory and `0600` database; it is not placed on the repository's NTFS/FUSE mount.
- The SQLite schema is versioned and migrated transactionally; a failed migration leaves the previous database intact and blocks new mutations with a clear diagnostic.
- Existing `data/control-audit.jsonl` history is preserved as a legacy audit/export source rather than silently discarded.
- History retention is bounded and configurable without deleting game data or backups.

**Likely files:**

- Create: `operations.py`
- Create: `data/schema.sql` (schema source only; live DB goes under XDG state)
- Modify: `app.py`
- Modify: `hermes-plugin/dashboard/plugin_api.py`
- Modify: `desktop-plugin/plugin.js`
- Modify: `static/app.js`
- Test: `tests/test_operations.py`
- Test: `tests/test_plugin_api.py`

---

### GHC-006 — Backup evidence and guarded recovery (P0)

**Story:** As a server operator, I want to create, verify, inspect, and safely restore backups for fully managed games, so lifecycle control does not put worlds and saves at needless risk.

**Why this is required:** Minecraft has a create-backup command and property changes create rollback files, but there is no operator-visible backup inventory or restore/undo workflow. Palworld status can find a backup, but its profile exposes no backup control.

**Acceptance criteria:**

- Fully managed games declare typed backup inventory, create, verify, and restore capabilities; unsupported games say so explicitly.
- Minecraft and Palworld v1 adapters both expose the latest backup timestamp, size, validation result, and source save/world identity.
- A successful backup operation proves a new artifact exists, is non-empty, and passes adapter-specific archive validation.
- Configuration changes expose **Undo** using the exact rollback artifact created by that operation.
- World/save restore requires the server to be stopped, an explicit artifact selection, a preview of what will be replaced, typed disruptive confirmation, and a pre-restore safety backup.
- Restore is path-confined, rejects symlink/traversal artifacts, preserves permissions, and verifies the server can read the restored data before Start is re-enabled.
- Backup retention never deletes the only known-good artifact and failures are truthful.
- Restore tests run only against disposable fixtures and prove an outside sentinel remains unchanged.

**Likely files:**

- Modify: `game_adapters.json`
- Modify: `schemas/game-adapter-config.schema.json`
- Create: `backups.py`
- Modify: `control_engine.py`
- Modify: `desktop-plugin/plugin.js`
- Modify: `static/app.js`
- Test: `tests/test_backups.py`

---

### GHC-007 — Safe logs and diagnostics (P1)

**Story:** As a server operator, I want recent bounded logs and actionable diagnostics inside the console, so I can understand a failed launch without opening a terminal or exposing secrets.

**Why this is required:** The backend collects a narrow Minecraft log pulse but neither UI displays it, and other games provide no diagnostics. Apply failures show only captured script output from that request.

**Acceptance criteria:**

- Adapters declare approved log files relative to the confined game directory.
- The API returns a bounded tail with timestamps and truncation metadata; it cannot accept arbitrary paths.
- Redaction covers IPs where configured, tokens, passwords, common secret assignments, and control characters.
- The UI has a per-game Diagnostics section showing readiness blockers, collector failures, active operation, recent action output, and recent approved logs.
- A **Copy diagnostics** action produces a redacted text bundle with versions and capability state, not raw saves/configuration/secrets.
- Empty, missing, huge, binary, rotated, and permission-denied logs have explicit safe states and tests.

**Likely files:**

- Create: `diagnostics.py`
- Modify: `app.py`
- Modify: `game_adapters.json`
- Modify: `hermes-plugin/dashboard/plugin_api.py`
- Modify: `desktop-plugin/plugin.js`
- Modify: `static/app.js`
- Test: `tests/test_diagnostics.py`

---

### GHC-008 — Enforced loopback/authentication boundary (P0)

**Story:** As a server operator, I want mutation controls reachable remotely only through Hermes authentication, so an accidental bind or old LAN-forwarding script cannot expose Start/Stop/Restore to the network.

**Why this is required:** Loopback is only a default. `app.py --host 0.0.0.0` can currently expose an unauthenticated mutation API, while old LAN-enablement artifacts remain in the repository.

**Acceptance criteria:**

- The mutation service refuses non-loopback binds in v1.
- No environment variable or convenience script silently weakens that rule.
- The Hermes bridge keeps an explicit method/path allow-list for status, catalog, operations, diagnostics, plan, and apply.
- Mutation audit source is server-assigned from the authenticated bridge/local surface rather than trusted solely from arbitrary request text.
- Body and response limits remain enforced on every proxied endpoint.
- The unrelated architecture ZIP download and obsolete LAN-enablement artifacts are removed from the runtime surface or clearly isolated from the mutation service.
- Tests prove non-loopback startup refusal, unapproved proxy path rejection, missing/incorrect confirmation rejection, mandatory digest rejection, and authenticated route registration.

**Likely files:**

- Modify: `app.py`
- Modify: `hermes-plugin/dashboard/plugin_api.py`
- Modify: `tests/test_app_api.py`
- Modify: `tests/test_plugin_api.py`
- Review/remove: `Enable-GameHostDashboard-LAN-Admin.ps1`
- Review/remove: `RUN-AS-ADMIN-Enable-GameHostDashboard-LAN.cmd`
- Review/remove: `downloads/project-architecture-inventory.zip`

---

### GHC-009 — Resilient, reversible console installation (P1)

**Story:** As a server operator, I want the console and Hermes plugin to survive login/reboot and update cleanly, so the control plane is available when needed without a mystery tmux/nohup process.

**Why this is required:** The current service is a manually started tmux/nohup process. Install snapshots existing plugin files, but there is no managed service unit, deployment manifest, automatic rollback command, or full lifecycle test.

**Acceptance criteria:**

- Install creates a user-owned service definition with loopback-only `ExecStart`, explicit environment paths, bounded `Restart=on-failure`, and journal/file logging.
- Start/stop/status scripts delegate to the managed service and remain idempotent.
- Install/update validates prerequisites and the registry before replacing the live plugin.
- Install writes a deployment manifest with version and file hashes; rollback can restore the immediately previous plugin deployment.
- Uninstall disables the plugin and service while preserving source, operation DB, audit history, game files, configs, and backups.
- Reinstall does not overwrite operator-owned game configuration or duplicate services/plugins.
- Lifecycle tests use a disposable Hermes home and prove unrelated/default plugins remain unchanged.
- Documentation matches current Desktop plugin behavior: hot reload first, command-palette reload only as fallback, and dashboard backend restart only when its mounted Python routes change.

**Likely files:**

- Create: `packaging/systemd/hermes-game-host-console.service`
- Modify: `install-hermes-plugin.sh`
- Modify: `uninstall-hermes-plugin.sh`
- Modify: `start.sh`
- Modify: `stop.sh`
- Modify: `status.sh`
- Create: `tests/test_install_lifecycle.py`
- Modify: `README.md`

---

### GHC-010 — v1 doctor, accessibility, and release gate (P0)

**Story:** As a server operator, I want one read-only doctor command and a tested UI, so I can know whether the console is safe to use before pressing a mutation control.

**Why this is required:** Existing tests cover core preview/apply behavior but not adapter schema enforcement, readiness, protocol accuracy, full lifecycle safety, installer rollback, or meaningful browser behavior. The static UI accessibility snapshot also loses useful names for some form controls.

**Acceptance criteria:**

- `./doctor.sh` is read-only and reports service health, loopback bind, registry validation, per-game readiness/capabilities, script permissions, data-store health, plugin discovery/enablement, and mounted authenticated routes without printing secrets.
- Every form control has a stable accessible name and keyboard path; dialogs trap/restore focus and announce errors/progress.
- Desktop and standalone UIs have behavioral tests for readiness gating, degraded status, preview, confirmation, reconnecting to an active operation, error recovery, and durable history.
- Test matrix includes reject paths, stale plans, wrong actor/source, wrong/missing digest, expiration cleanup, concurrent operations, failed postconditions, redaction, backup/restore sentinels, wrong-target uninstall, and non-interference.
- A clean release command runs Python, bridge-venv, Node, schema, and browser smoke checks and exits nonzero on any failure.
- Python/runtime dependencies and supported versions are declared in a project manifest; install/doctor report missing or incompatible dependencies before starting the service.
- All v1 source, schemas, scripts, profiles, tests, and plugin artifacts are tracked; generated logs, PID files, caches, deployment backups, operation databases, and downloaded archives are ignored.
- `git diff --check` and the repository's release checks are clean before a version tag is created.
- `README.md`, `STATUS.md`, and `docs/ADDING_A_GAME.md` describe the same actual v1 support boundary and green-path commands.
- Version is advanced consistently across backend server header, plugin metadata, manifest, and status documentation only after the release gate passes.

**Likely files:**

- Create: `doctor.sh`
- Create: `pyproject.toml`
- Create: `scripts/release-check.sh`
- Create: `tests/browser/`
- Modify: `.gitignore`
- Modify: `tests/ui.test.js`
- Modify: `tests/desktop_plugin.test.js`
- Modify: `README.md`
- Modify: `STATUS.md`
- Modify: `docs/ADDING_A_GAME.md`

---

### GHC-011 — Persistent restart-required and effective-value state (P1)

**Story:** As a server operator, I want the console to distinguish a value that was written from a value the running server has actually adopted, so I do not mistake saved configuration for live configuration.

**Why this is required:** Minecraft controls are marked `restartRequired`, but the catalog immediately rereads the edited properties file and presents the new value as current. The warning is control-local and transient; no durable server-level pending-restart state survives a reload.

**Acceptance criteria:**

- A successful restart-required configuration operation creates durable pending state tied to the game, control, originating operation, configured value, and timestamp.
- Status and UI distinguish **Configured value** from **Effective value**; when the effective value cannot be queried, it is shown as **Unknown until next verified start/restart**, never silently equated with the configured value.
- A server-level **Restart required** banner lists pending changes and offers the existing guarded Restart action when the server is running.
- If the server is stopped, the UI says the setting will apply on the next start instead of demanding an immediate restart.
- Pending state survives browser and console-service restarts and clears only after a verified start/restart that occurred after the latest pending change.
- Editing the same control again updates the pending configured value while preserving the earlier operation records in history.
- Tests cover running, stopped, degraded, repeated-edit, failed-restart, service-restart, and successful verified-restart cases.

**Likely files:**

- Modify: `control_engine.py`
- Modify: `operations.py`
- Modify: `app.py`
- Modify: `desktop-plugin/plugin.js`
- Modify: `static/app.js`
- Test: `tests/test_operations.py`
- Test: `tests/ui.test.js`

---

## Reviewer reconciliation

Three read-only workstreams reviewed product/UX, backend safety, and release readiness. The main review re-ran the material checks before accepting them into this plan.

| Recommendation | Disposition in this plan | Reason |
|---|---|---|
| Keep v1 to Minecraft + Palworld | **Accepted** | These are the only installed server projects with real approved scripts; the other seven remain disabled setup templates. |
| Do not add live console streaming or a general action queue | **Accepted with clarification** | v1 gets bounded diagnostic snapshots and durable per-game operation state, not arbitrary command streaming, scheduling, or a user-managed queue. |
| Require stale-plan, digest, backend-precondition, path, and partial-failure defenses | **Accepted as P0** | Isolated probes reproduced every failure mode against the current engine. |
| Add audit/history and recovery | **Accepted** | Current success-only JSONL is not enough to explain failures or use the generated rollback artifacts. |
| Persist restart-required state and distinguish configured from effective values | **Accepted as P1** | The current UI rereads the edited file and can imply that a running server already adopted a setting that actually needs a restart. |
| Add backup/restore to v1 | **Accepted conditionally** | A game is labeled fully managed only if its adapter-specific restore path passes disposable sentinel tests and makes a pre-restore backup. |
| Replace terminal-scoped runtime with a managed service | **Accepted** | This is availability infrastructure, not a watchdog that hides product bugs; restart behavior remains bounded and visible. |
| Add installer/update reproducibility, dependency declaration, and release checks | **Accepted as P0** | The current in-progress tree passes tests but cannot be reproduced from `HEAD` because critical artifacts are untracked. |
| Add SteamCMD installers, RCON, metrics history, scheduling, mod management, or multi-host control | **Deferred** | Useful later, but they widen the blast radius without fixing v1 trustworthiness. |

---

## Implementation order after approval

1. **Freeze and expand the green path:** add a single release-check command; preserve all current passing behavior.
2. **GHC-001:** validated/confined registry.
3. **GHC-002:** readiness and capability model; immediately stop advertising fake actions.
4. **GHC-003:** truthful telemetry and endpoint configuration.
5. **GHC-008:** lock the network/auth boundary before widening the API.
6. **GHC-004 + GHC-005:** durable verified operations, one vertical lifecycle slice at a time.
7. **GHC-011:** persistent restart-required and configured-versus-effective state.
8. **GHC-006:** backup evidence, config undo, then disposable-fixture restore.
9. **GHC-007:** bounded diagnostics/log views.
10. **GHC-009:** managed service and reversible deployment.
11. **GHC-010:** browser/accessibility coverage, doctor, docs, versioning, and final release gate.

Every implementation slice follows RED → GREEN → REFACTOR and ends with the full current green path. No real running game server is stopped, restarted, or restored merely to prove a test.

## Current green path to preserve

```bash
cd "/path/to/h-realms"
python3 -m unittest discover -s tests -v
node tests/ui.test.js
node tests/desktop_plugin.test.js
/path/to/hermes-agent/venv/bin/python -m unittest tests.test_plugin_api -v
curl -fsS http://127.0.0.1:5057/health
```

## v1 release definition of done

- All P0 and P1 stories above are accepted or explicitly descoped by Joe.
- Minecraft and Palworld pass real, non-destructive readiness/status/backup verification on this host.
- Catalog-only games cannot run mutations and clearly explain setup requirements.
- No hardcoded personal IP/network fallback remains.
- Non-loopback mutation binding is impossible in the v1 runtime.
- Successful lifecycle toasts are backed by verified postconditions.
- Failed/interrupted operations remain visible after UI/service restart.
- Restart-required changes remain visible until a later verified start/restart, and configured values are never mislabeled as effective values.
- Config undo and supported-game restore paths pass disposable-fixture safety tests.
- Managed install/update/uninstall preserve game data, operation history, audit, and backups.
- Doctor and the complete release gate pass.
- The plan is reviewed before any product-code implementation begins.

## Decisions Joe should review

1. **Support boundary:** approve Minecraft + Palworld as the only fully managed v1 games; keep the other seven as disabled setup templates.
2. **Recovery boundary:** approve tested restore support as a v1 requirement rather than backup-create only.
3. **Runtime boundary:** approve a user service (systemd on this Linux host) instead of tmux/nohup as the supported v1 runtime.
4. **Network boundary:** approve loopback-only mutation service with Hermes as the sole authenticated remote bridge.
5. **Scope discipline:** keep SteamCMD install/update, RCON, metrics history, and multi-host control out of v1.
