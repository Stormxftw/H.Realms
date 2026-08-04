---
name: hermes-game-host-console
description: "Use when creating or customizing Game Host Console servers."
version: 1.1.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [gaming, game-server, profiles, controls, adapters]
---

# Hermes Game Host Console — Server Profile Builder

Use this skill when the user asks Hermes to add, customize, or remove a dedicated game server in Hermes Game Host Console.

The repository deliberately ships only Minecraft and Palworld as examples. New servers are local customizations generated from the same declarative contracts; do not add executable authority to remote profile data.

## Safety rules

- Inspect the active repository before editing it. Do not assume a machine path.
- Preserve server files, saves, backups, credentials, and existing local configuration.
- Keep credentials and machine-specific paths out of tracked files.
- Treat profiles and adapters as a matched pair: profile ID, adapter key, controls, actions, and property types must agree.
- Profiles are JSON data, not executable code.
- Only local scripts beneath the configured projects root may start, stop, restart, or back up a server.
- Never invent a publisher download URL, Steam App ID, port, process name, configuration format, or shutdown procedure. Verify those details from official sources.
- Never download or launch a server binary without explicit user approval.

## Files

| Path | Purpose |
|---|---|
| `game_profiles/` | Tracked Minecraft and Palworld examples |
| `game_adapters.json` | Tracked example adapters |
| `data/local-game-profiles/` | Ignored local profiles created for this machine |
| `data/local-game-adapters.json` | Ignored local adapter registry |
| `schemas/` | Strict profile and adapter contracts |
| `catalog/packages/` | PR-reviewed Store examples and community submissions |

`start.sh` automatically uses both ignored local paths when they exist. Explicit `GAME_HOST_PROFILES_DIR` and `GAME_HOST_ADAPTER_CONFIG` values take precedence.

## Create a local server profile

1. Locate the repository root by finding `profile_store.py`, `game_adapters.json`, and `schemas/`.
2. Inspect the closest shipped example. Use Minecraft for property-driven servers and Palworld for process/REST-oriented servers.
3. If local configuration does not exist yet:
   - create `data/local-game-profiles/`;
   - copy the shipped Minecraft and Palworld profile JSON files into it;
   - copy `game_adapters.json` to `data/local-game-adapters.json`.
4. Research the requested dedicated server using official publisher or platform documentation. Record the source URLs in your response.
5. Create `data/local-game-profiles/<game-id>.json` using `game_profiles/_template.json` and the profile schema.
6. Add the matching entry to `data/local-game-adapters.json` using the adapter schema.
7. Create or adapt the local server directory and lifecycle scripts only after confirming the user's projects root and obtaining approval for downloads or process-changing actions.
8. Validate the profile, adapter registry, referenced actions, property types, paths, ports, and script existence.
9. Run focused tests, restart the console, and verify the game through `/api/store`, `/api/controls`, and `/api/status`.
10. Tell the user exactly what was created, what still requires publisher files or credentials, and how to roll back.

## Control contract

Supported kinds are `button`, `switch`, `slider`, `select`, `text`, `number`, and `readonly`.

Common actions:

- `ui.refresh`
- `service.start`
- `service.stop`
- `service.restart`
- `backup.create`
- `property.set`

Every non-UI action used by a profile must exist in its adapter. Every `property.set` key must exist in `propertyTypes`. Risk levels must not understate the runtime action.

## Verification

From the repository root:

```bash
uv run --no-project --with jsonschema python3 -m unittest tests.test_registry tests.test_control_engine -v
uv run --no-project --with jsonschema python3 scripts/build-profile-catalog.py --check
./stop.sh && ./start.sh
curl -fsS http://127.0.0.1:5057/health
```

Then inspect the relevant game in:

```bash
curl -fsS http://127.0.0.1:5057/api/store
curl -fsS http://127.0.0.1:5057/api/controls
curl -fsS http://127.0.0.1:5057/api/status
```

A server need not be online to pass configuration validation, but the console must start cleanly and report an honest offline/readiness state.

## Publishing a profile

Local customization is the default. If the user explicitly wants to contribute the profile:

1. Read `CONTRIBUTING.md`.
2. Convert the local profile and adapter into `catalog/packages/<game-id>.json`.
3. Community packages must use `community/<game-id>` and the fixed local script slots `start.sh`, `stop.sh`, and `backup.sh`.
4. Do not include scripts, secrets, absolute paths, mutable-property authority, or server binaries.
5. Rebuild the deterministic catalog and open a pull request with verification evidence.
