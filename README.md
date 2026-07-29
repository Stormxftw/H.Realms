# Hermes Game Host Console

Hermes-native control surface for local dedicated game servers (Minecraft + Palworld first).

Profiles describe controls. Hardcoded Python adapters execute approved actions. Every mutation is previewed, confirmed, audited, and reversible where possible.

## What you get

| Surface | Path / URL |
|---|---|
| Local console | `http://127.0.0.1:5057` |
| Hermes Desktop page | `/game-host` |
| Authenticated API bridge | `/api/plugins/game-host-console/` on the Hermes dashboard |
| Profiles | `game_profiles/minecraft.json`, `game_profiles/palworld.json` |
| Schema | `schemas/game-control-profile.schema.json` |

## Control model

Closed kinds only:

`button` · `switch` · `slider` · `select` · `text` · `number` · `readonly`

Flow:

1. UI requests `POST /api/control/plan`
2. Operator reviews preview (risk, current → proposed, restart required)
3. UI requests `POST /api/control/apply` with `confirmed=true`, same `actor`, and `planDigest`
4. Engine consumes the one-time plan, runs only approved adapters, writes audit + rollback material

Profiles **cannot** embed shell/commands. Only backend adapters can run:

- Minecraft: `start.sh`, `stop.sh`, `backup.sh`, selected `server.properties` keys
- Palworld: `scripts/palworld-linux-start.sh`, `scripts/palworld-linux-stop.sh`

## Safety

- Loopback bind by default (`127.0.0.1:5057`)
- No mutation without explicit confirmation
- Actor + digest binding on apply
- Unknown profile/control fields rejected
- Hermes bridge is session-authenticated
- Do not LAN-forward `:5057` while controls are enabled

## Install

```bash
cd "/path/to/h-realms"
./start.sh
./install-hermes-plugin.sh
```

Then in Hermes Desktop:

1. Command palette → **Reload desktop plugins**
2. Open **Game Host** in the sidebar

Uninstall:

```bash
./uninstall-hermes-plugin.sh
./stop.sh
```

## Develop / test

```bash
python3 -m unittest discover -s tests -v
node tests/ui.test.js
node tests/desktop_plugin.test.js
# FastAPI bridge test needs Hermes venv:
/path/to/hermes-agent/venv/bin/python -m unittest tests.test_plugin_api -v
```

## Layout

```text
app.py                 local HTTP service
control_engine.py      profile validation + plan/apply/audit
game_profiles/         declarative non-executable UI profiles
static/                standalone web UI
desktop-plugin/        Hermes Desktop Plugin SDK entry
hermes-plugin/         authenticated dashboard bridge (plugin_api.py)
tests/                 python + node tests
```

## Hermes generation of controls

Hermes (or you) may edit/create JSON profiles under `game_profiles/`. Keep them schema-valid and free of executable fields. The engine will refuse unknown keys and unknown control kinds.
