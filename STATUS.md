# Hermes Game Host Console Status

- Status: **Working MVP — Installed/Store model + accurate live status**
- Latest verification: `2026-08-03 EDT` — health check: backend under systemd, full suite green (167 passed, 184 subtests)
- Product/add-on name: `Hermes Game Host Console`
- Version: backend `HermesGameHostConsole/0.4`, plugin `0.3.0`
- Port: `5057` (loopback only by default)
- Local URL: `http://127.0.0.1:5057`
- Hermes Desktop route: `/game-host`
- Authenticated bridge: `/api/plugins/game-host-console/*` on the Hermes dashboard (`:9119`)

## What works

- Live local console with **9 supported game profiles** (Minecraft, Palworld, Valheim, CS2, Terraria, Don't Starve Together, Satisfactory, Enshrouded, Sons of the Forest)
- **Installed vs Store model** — the sidebar shows only games you have *installed*; the rest live in the **Store** until you add them (from the Store UI or via the Hermes `game-host-console` skill). Seeded on first run from what is present/running.
- **Accurate live status** — a running process whose listeners are listening reports `RUNNING`, even if setup blockers are pending (setup gates *mutations*, not runtime health)
- **Player counts** — Minecraft, Source (A2S) and Palworld REST show live `players`; Palworld shows real counts once its REST API is enabled
- Closed control kinds: button, switch, slider, select, text, number, readonly
- Declarative profiles in `game_profiles/*.json` (no executable code in profiles)
- **Data-driven adapter mapping** in `game_adapters.json` — adding a new game no longer requires edits to `control_engine.py`
- Plan → confirm → apply flow with one-time plan IDs, actor binding, plan digest, audit log, property rollback copies
- Hermes Desktop disk plugin installed at `~/.hermes/desktop-plugins/game-host-console/`
- Hermes backend bridge installed at `~/.hermes/plugins/game-host-console/`
- Plugin listed by dashboard discovery with `has_api: true`

## New: Installed/Store + accuracy pass (August 2, 2026)

### Accurate runtime status
- `aggregate_state` now reports `running_ready` whenever the process is running and all declared listeners are listening — setup/readiness blockers no longer downgrade a healthy server to `DEGRADED`
- `store.py` — `InstalledStore` persists the installed game set at `data/installed.json` (atomic, isolated from tests)
- `/api/store` lists supported but non-installed games; `POST /api/store/install` and `POST /api/store/uninstall` (actor-bound) scaffold a confined project home and flip the installed flag — server files are kept on uninstall
- Catalog (`/api/controls`) now carries `installed` per game and `installedIds`
- Bridge proxy allowlist extended for store GET/POST; Desktop plugin redesigned to show installed servers only with a Store panel
- New `palworld_rest` status collector (best-effort live player counts; fails soft when the server REST API is off)

### Works on any machine — operation gates on the contract, not the layout
- **Mutation capability gates on the operational contract**: project dir real + the game's action scripts (`start.sh`/`stop.sh`) present and executable. Where the game binary lives (internal vs external install) no longer matters — the scripts are the user's own and describe it.
- **`requiredPaths` are now advisory "hints"**, surfaced as a muted `hints` array alongside the catalog — they no longer block Stop/Restart for a server the scripts can already run (fixes the Palworld false "setup needed").
- **Only capabilities a profile's controls actually use are enforced**, so an empty `propertyTypes` for a game with no config control no longer produces a false `misconfigured` blocker.
- **Clear, machine-agnostic fix messaging**: missing-project and missing-script blockers now say exactly what to do ("create the project directory with action scripts… or install from the Store / the Hermes game-host-console skill").

### Notes / requirements
- Live Palworld player counts require enabling the server's REST API: `RESTAPIEnabled=True`, `bShowPlayerList=True` in `PalWorldSettings.ini`, then a server restart. RCON is off and not used.
- Install creates only the project home + `PROVISION.md`; actual server files/scripts are provisioned via the Hermes skill or manual setup.
- After reinstalling the plugin, reload Desktop plugins from the command palette and restart the Hermes gateway to pick up the new bridge routes.

## New in Wave 2 (July 30, 2026)

### Durable lifecycle operations
- **Serialized per-game mutations** — no more race conditions between start/stop/restart
- **Operation store** (SQLite) — every mutation is recorded with state, actor, timestamps, and postcondition checks
- **Async apply pattern** — plan → confirm → submit → poll for completion via `/api/operations/{id}`
- **Restart-required state** — persistent tracking of config changes that need a server restart to take effect

### Backup foundation
- **Path-confined backup inventory** — create, list, validate, preview, restore, and prune backups
- **Guarded restore** — stopped-state enforcement and exact confirmation required
- **Retention pruning** — automatic cleanup that never touches game files

### Diagnostics foundation
- **Bounded diagnostics** — collect system and game-specific diagnostic bundles
- **Safe output limits** — truncation and redaction of sensitive data

### Test coverage
- **154 tests passing** (1 pre-existing Hermes-loader discovery failure unrelated to project code)
- Full lifecycle integration tests for start/stop/restart/configure operations
- Backup and restore round-trip tests
- Operation recovery and retention tests
- New `test_store_api.py` covering installed persistence, catalog/store, and install/uninstall scaffolding


## New in Wave 1 (July 28, 2026)

### Data-driven adapter architecture
The old hardcoded `_commands_for()` dict is gone. `game_adapters.json` now maps:

| Field | Purpose |
|---|---|
| `projectDir` | Subdirectory under PROJECTS_ROOT with start.sh / stop.sh |
| `commands` | Action → [[script, timeout], ...] mapping |
| `propertyTypes` | Mutable server.properties key → type mapping (if any) |
| `statusCollector` | `minecraft_ping`, `steam_query`, or `process_only` |
| `processSearch` | Substring for /proc/cmdline detection |
| `defaultPort` | Game port for status display |

### New game profiles added
- **Valheim** — Steam AppID 896660, anonymous SteamCMD, UDP 2456
- **Counter-Strike 2** — Steam AppID 730, needs GSLT token, UDP 27015
- **Don't Starve Together** — Steam AppID 343050, anonymous SteamCMD, UDP 10999
- **Satisfactory** — Steam AppID 1690800, anonymous SteamCMD, UDP 7777
- **Enshrouded** — Steam AppID 2278520, anonymous SteamCMD, UDP 15636
- **Sons of the Forest** — Steam AppID 2465200, anonymous SteamCMD, UDP 8766
- **Terraria** — Standalone binary, TCP 7777

### Generic status collectors
- `steam_a2s_info()` — Source engine A2S_INFO UDP query for server name, map, player count
- `generic_server_status()` — Reads adapter config, auto-detects process, reports port listeners
- `dashboard_data()` now dynamically iterates all adapters instead of hardcoding Minecraft+Palworld

### Documentation
- `docs/ADDING_A_GAME.md` — Complete 3-step guide for adding new games
- `game_profiles/_template.json` — Copy-paste starter profile
- `schemas/game-adapter-config.schema.json` — Formal schema for the adapter config

## Safety model (unchanged)

- Profiles describe UI semantics only
- Mutations require `confirmed=true`
- Apply actor must match plan actor
- Optional/required `planDigest` must match when supplied
- Property writes use `server.properties` pattern with pre-write backup
- Local service binds `127.0.0.1` by default; Hermes proxy is auth-gated
- Adapter config maps to approved scripts only — no arbitrary shell from profiles
- Do **not** expose port `5057` on the LAN while mutation endpoints are enabled

## Active game servers on this host

| Game | Status | Scripts exist? |
|---|---|---|
| Minecraft | Running | Yes (minecraft-server/) |
| Palworld | Running | Yes (palworld-server-local/) |
| Valheim | Not installed | Needs start.sh/stop.sh + SteamCMD download |
| CS2 | Not installed | Needs GSLT token + start.sh/stop.sh |
| Terraria | Not installed | Needs binary + world file |
| DST | Not installed | Needs cluster token + start.sh/stop.sh |
| Satisfactory | Not installed | Needs SteamCMD download + start.sh/stop.sh |
| Enshrouded | Not installed | Needs SteamCMD download + start.sh/stop.sh |
| SotF | Not installed | Needs SteamCMD download + start.sh/stop.sh |

## How to add a game server for real

1. **Create the server directory**: `~/Projects/<game>-server/`
2. **Write start.sh and stop.sh** — see `docs/ADDING_A_GAME.md` for patterns
3. **Download the server files** — via SteamCMD or direct download
4. **The profile + adapter are already done** — the new game appears in the console immediately

## Test verification (`2026-07-30`)

- `python -m pytest tests/ -q` → **146 passed, 0 failed** (1 pre-existing plugin loader test deselected)
- Full lifecycle integration tests for start/stop/restart/configure operations
- Backup and restore round-trip tests
- Operation recovery and retention tests
- Restart-required state persistence tests
- Truthful readiness and telemetry tests

## Reverse / uninstall

```bash
cd "/run/media/zim/a drive/Hermes/Projects/game-host-dashboard"
./uninstall-hermes-plugin.sh
./stop.sh
```

## Next useful upgrades (optional)

- SteamCMD helper function in app.py for auto-updating servers
- Auto-create start.sh/stop.sh stubs for new profiles
- RCON-backed readouts for games that support it
- Historical metrics in SQLite