<p align="center">
  <img src="assets/branding/hermes-game-host-console-banner.webp" alt="Hermes Game Host Console — a dark arcane operations console surrounded by game-server, telemetry, verification, backup, and security symbols" width="100%">
</p>

# Hermes Game Host Console

<p align="center">
  <strong>A guarded control plane for dedicated game servers.</strong><br>
  One console for lifecycle controls, typed configuration, live telemetry, backups, diagnostics, and verified community profiles.
</p>

> [!IMPORTANT]
> This project controls real server processes and files. It binds to loopback by default, requires explicit confirmation for mutations, and does not ship game binaries, publisher credentials, or server tokens.

Hermes Game Host Console turns a folder of dedicated servers into a typed, inspectable operations surface. It runs as a local Python service, appears as a native Hermes Desktop page, and exposes its browser-facing API only through an authenticated Hermes plugin bridge.

The design is deliberately conservative: profiles describe intent, backend adapters define the narrow operations that may occur, and every meaningful mutation passes through **plan → review → confirm → apply**. A control panel should be powerful. It should not be reckless.

## At a glance

| Capability | What it does |
|---|---|
| Native Hermes Desktop UI | Adds the **Game Host** route, sidebar entry, command-palette action, loading/error states, confirmation dialogs, Store, backups, and diagnostics using the Hermes Plugin SDK. |
| Standalone local console | Serves the core UI and API at `http://127.0.0.1:5057`. |
| Nine bundled game profiles | Minecraft, Palworld, Valheim, Counter-Strike 2, Terraria, Don't Starve Together, Satisfactory, Enshrouded, and Sons of the Forest. |
| Typed controls | Closed control kinds: `button`, `switch`, `slider`, `select`, `text`, `number`, and `readonly`. |
| Guarded mutations | One-time plans, actor binding, plan digests, explicit confirmation, per-game serialization, postcondition checks, and audit records. |
| Durable operations | SQLite-backed queued/running/terminal operation states, polling, interrupted-operation recovery, and truthful `outcome_unknown` handling. |
| Live server status | Process and listener probes, readiness blockers, ports, player counts, uptime, and resident memory. |
| Game-aware telemetry | Minecraft ping, Source A2S, Palworld REST, Steam/process probes, and best-effort failure boundaries. |
| Backup and restore | Verified inventories, bounded `.tar.gz` creation, restore preview tokens, stopped-server enforcement, safety backups, and atomic rollback. |
| Diagnostics | Approved log inventory, bounded tails, secret/IP/path redaction, and downloadable diagnostic bundles. |
| Installed/Store model | Shows installed games in the sidebar and keeps the remaining profiles in a dedicated Store. Uninstall keeps server files. |
| Verified GitHub profile catalog | Community packages arrive through pull requests, deterministic CI, strict JSON validation, size limits, SHA-256 verification, and offline fallback. |
| Repository-packaged game art | Local allowlisted WebP artwork with dimensions, digests, provenance, licensing, and graceful fallback—no publisher CDN calls from Desktop. |

## How the system fits together

```mermaid
flowchart LR
    U[Operator] --> D[Hermes Desktop plugin]
    U --> W[Standalone local UI]
    D -->|authenticated plugin API| B[Hermes bridge]
    B -->|loopback only| A[Game Host service]
    W --> A

    A --> C[Control engine]
    A --> S[Installed / Store state]
    A --> T[Telemetry collectors]
    A --> K[Backup + diagnostics]

    C -->|plan → confirm → apply| P[Approved local scripts and properties]
    C --> O[(Operation store + audit)]

    G[GitHub pull request] --> V[Catalog CI verification]
    V --> I[catalog/index.json]
    I -->|pinned source + SHA-256| S
    S -->|verified JSON only| C
```

### Trust boundaries

1. **The browser never chooses a command.** It submits a game ID, control ID, and typed value.
2. **Profiles contain data, not executable code.** Unknown fields, control kinds, actions, and property bindings are rejected.
3. **Community adapters do not choose executable authority.** New IDs are confined to `community/<game-id>` and may only use the fixed local slots `start.sh`, `stop.sh`, and `backup.sh`; no script is downloaded.
4. **Mutations are two-stage.** Planning is read-only; applying requires the same actor, the one-time plan ID, the plan digest, and explicit confirmation.
5. **Remote catalog data is untrusted until proven otherwise.** The service pins the official raw-GitHub path, bounds downloads, validates schemas and semantic bindings, checks exact package size and SHA-256, then stores an atomic verified cache.
6. **Failures close safely.** A bad or unavailable remote catalog never replaces known-good data; the app retains its verified cache or bundled catalog.

## Supported games

| Game | Bundled status strategy | Typical lifecycle contract |
|---|---|---|
| Minecraft Java | Minecraft ping + process/listener probes | `start.sh`, `stop.sh`, backup, selected `server.properties` keys |
| Palworld | Authenticated local REST + process/listener probes | Start/stop scripts, player count, max players, backup source mapping |
| Valheim | Steam/process status | Confined start/stop scripts |
| Counter-Strike 2 | Source A2S + process status | Confined start/stop scripts; operators provide their own GSLT |
| Terraria | Process/listener status | Confined start/stop scripts |
| Don't Starve Together | Steam/process status | Confined start/stop scripts; operators provide their own cluster token |
| Satisfactory | Steam/process status | Confined start/stop scripts |
| Enshrouded | Steam/process status | Confined start/stop scripts |
| Sons of the Forest | Steam/process status | Confined start/stop scripts |

A profile appearing in the Store does **not** mean its server files are bundled. Installing a profile creates a project home and `PROVISION.md`; you still obtain the dedicated-server files from the game publisher and provide your own local scripts and credentials.

## Control lifecycle

```text
operator input
    ↓
POST /api/control/plan
    ↓
validated typed proposal
    ↓
preview: risk, current → proposed, restart requirement
    ↓
explicit confirmation
    ↓
POST /api/control/apply
    ↓
one-time plan + actor + digest verification
    ↓
serialized operation → approved adapter → postcondition check
    ↓
audit record + rollback material where applicable
```

The operation API records queued, running, succeeded, failed, cancelled, and outcome-unknown states. Desktop polling is abort-aware and stops when the plugin unloads.

## Backups and diagnostics

### Backup flow

- Enumerate only configured backup sources.
- Create a bounded, verified `.tar.gz` artifact.
- Preview a restore without changing files.
- Require the game server to be stopped.
- Require the exact one-time confirmation token from the preview.
- Create a safety backup before restore.
- Roll back atomically if replacement fails.
- Prune according to retention without touching live game files.

### Diagnostic flow

- List only approved game log IDs.
- Tail bounded content instead of streaming unbounded files.
- Redact credentials, authorization values, private paths, control characters, and—by default—IP addresses.
- Build a deterministic diagnostic bundle containing status, blockers, telemetry, operations, and approved log excerpts.
- Fail softly when a process or file disappears during collection.

## Community profile Store

The Store is repository-backed rather than upload-backed. That distinction matters.

```text
contributor package
    → GitHub pull request
    → CODEOWNERS review
    → schema + semantic validation
    → deterministic index rebuild
    → Store/API tests
    → merge to main
    → official catalog feed
    → bounded download + SHA-256 verification
    → local install
    → one Game Host Console restart
    → active profile
```

Contributors edit `catalog/packages/<game-id>.json`. GitHub Actions then verifies:

- exact package envelope and allowed JSON fields;
- the existing control-profile and adapter schemas;
- profile ID, adapter ID, and package ID agreement;
- action and mutable-property bindings;
- relative, confined project/script paths;
- package size limits;
- deterministic `catalog/index.json` output;
- exact byte length and SHA-256 digest;
- focused Store loader and API tests;
- absence of stale generated catalog data.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the submission contract, evidence checklist, versioning rules, and explicit prohibition on executable content.

## Install

### Prerequisites

- Linux host
- Python 3.11+
- Hermes Agent/Desktop for the native plugin surface
- Node.js only for JavaScript tests
- Your own legally obtained dedicated-server files

### Clone and run

```bash
git clone https://github.com/Stormxftw/hermes-game-host-console.git
cd hermes-game-host-console
./start.sh
```

Health check:

```bash
curl -fsS http://127.0.0.1:5057/health
```

### Install the Hermes Desktop integration

```bash
./install-hermes-plugin.sh
```

Then:

1. Open the Hermes Desktop command palette.
2. Choose **Reload desktop plugins**.
3. Enable **Game Host Console** in Settings → Plugins if it is disabled.
4. Open **Game Host** from the sidebar or command palette.

The visible frontend is a single-file ESM plugin importing only `@hermes/plugin-sdk`, `react`, and `react/jsx-runtime`. The Python plugin is the authenticated backend bridge; it does not render the visible page.

### Uninstall

```bash
./uninstall-hermes-plugin.sh
./stop.sh
```

Uninstalling the console does not delete game-server files or backups.

## Configuration

Common environment variables:

| Variable | Purpose |
|---|---|
| `DASHBOARD_PORT` | Local service port; defaults to `5057`. |
| `HERMES_PROJECTS_ROOT` | Root beneath which relative game project directories are confined. |
| `GAME_HOST_ADAPTER_CONFIG` | Optional machine-local adapter mapping. Keep host-specific absolute paths out of Git. |
| `GAME_HOST_STORE_INDEX_URL` | Catalog source override; production code accepts only the approved repository path. |

Use a local, ignored adapter file for machine-specific paths. Public packages should remain portable and relative.

## Development and verification

Focused catalog check:

```bash
uv run --no-project --with jsonschema python3 scripts/build-profile-catalog.py --check
```

Complete Python suite:

```bash
uv run --no-project \
  --with pytest \
  --with fastapi \
  --with jsonschema \
  --with httpx \
  --with pyyaml \
  --with python-multipart \
  python3 -m unittest discover -s tests -v
```

Desktop behavior and contract tests:

```bash
node tests/ui.test.js
node tests/desktop_plugin.test.js
node --test tests/desktop_plugin_behavior.test.mjs
```

Full release gate:

```bash
uv run --no-project \
  --with pytest \
  --with fastapi==0.133.1 \
  --with jsonschema \
  --with httpx \
  --with pyyaml \
  --with python-multipart \
  bash scripts/release-check.sh
```

The release gate checks tracked artifacts, executable bits, generated-data exclusions, Python and Node suites, the Hermes bridge, schemas, catalog reproducibility, plugin installation smoke tests, and a clean Git archive.

## Repository map

```text
app.py                         local HTTP/API service
control_engine.py              schema validation + plan/apply/audit engine
profile_store.py               verified remote catalog, cache, install, activation
operations.py                  durable operation lifecycle
backups.py                     confined backup/restore manager
diagnostics.py                 bounded redacted diagnostic bundles
telemetry.py                   game-aware process/player/uptime/RSS probes
store.py                       installed-game state

game_profiles/                 bundled declarative control profiles
game_adapters.json             bundled adapter mappings
schemas/                       strict JSON Schemas
catalog/packages/               canonical community profile packages
catalog/index.json              deterministic generated catalog

desktop-plugin/plugin.js       native Hermes Desktop page
hermes-plugin/plugin_api.py     authenticated local API bridge
static/                         standalone local web surface
assets/game-art/                allowlisted local game artwork
assets/branding/                project identity artwork

scripts/build-profile-catalog.py deterministic catalog builder
scripts/release-check.sh         release-quality verification gate
tests/                           Python, Node, contract, and security tests
```

## Security posture

- Keep the service on loopback. Do not port-forward `5057`.
- Never commit publisher tokens, admin passwords, server configuration, saves, logs, or diagnostics bundles.
- Treat profile changes as security-sensitive even though they are declarative.
- Require the catalog CI check and maintainer review before merging community packages.
- Keep machine-specific absolute paths in ignored local configuration.
- Review generated diagnostics before sharing them; redaction reduces risk but does not grant infallibility.
- Report vulnerabilities privately to the maintainer rather than opening an issue containing exploit details or secrets.

## Project philosophy

The console is not trying to become a universal shell with prettier buttons. It is a narrow control plane: explicit contracts, visible consequences, recoverable operations, and enough telemetry to tell truth from noise.

The machine may be powerful. The operator should remain in command.

---

<p align="center"><sub>Banner artwork generated for this repository; provenance is documented in <a href="assets/branding/README.md">assets/branding/README.md</a>.</sub></p>
