# Adding a New Game Server

Three steps, no code changes to `control_engine.py` or `app.py`.

## Step 1 — Create the server scripts

Under `PROJECTS_ROOT/<game-id>-server/`, create at minimum:

```
<game-id>-server/
  start.sh    — launches the dedicated server as a background process
  stop.sh     — gracefully stops the server (SIGTERM, then SIGKILL if needed)
```

Conventions for scripts:
- `start.sh` should background the server (use `&` or `tmux`/`screen`)
- `stop.sh` should find the process by name and terminate it
- Scripts must be executable: `chmod +x start.sh stop.sh`
- Scripts must return exit code 0 on success
- Scripts capture their own stdout/stderr for audit (log to a `logs/` directory)

### Example start.sh (generic pattern)
```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p logs
./game_server_binary \
  -port 2456 \
  -world "Dedicated" \
  >>logs/server.log 2>&1 &
echo $! > server.pid
echo "Started (PID $(cat server.pid))"
```

### Example stop.sh (generic pattern)
```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [ -f server.pid ]; then
  kill "$(cat server.pid)" 2>/dev/null && echo "Stopped" || true
  rm -f server.pid
fi
# Fallback: kill by process name
pkill -f "game_server_binary" 2>/dev/null || true
```

---

## Step 2 — Create the game profile

Copy `game_profiles/_template.json` → `game_profiles/<game-id>.json`.

### Required fields
| Field | Purpose |
|---|---|
| `id` | Must match the game key in `game_adapters.json` (lowercase, hyphens, no spaces) |
| `name` | Display name shown in the sidebar and header |
| `controls[]` | At minimum: refresh, start, stop, restart |

### Control kinds available
| Kind | What it renders | Example use |
|---|---|---|
| `button` | Clickable button | Start, stop, restart, backup |
| `switch` | Toggle (true/false) | Whitelist on/off, PvP on/off |
| `slider` | Number range with step | Max players, view distance |
| `select` | Dropdown | Difficulty, game mode |
| `text` | Single-line text input | Server name, MOTD, password |
| `number` | Numeric input | Port, tick rate |
| `readonly` | Display-only value | Connected IP, version |

### Control risks (determines confirmation strictness)
| Risk | Meaning |
|---|---|
| `read-only` | No mutation, no confirmation needed |
| `safe` | Low-risk read operation |
| `safe-mutation` | Safe write (e.g., backup creation) |
| `configuration` | Config change, may need restart |
| `service` | Service lifecycle change |
| `disruptive` | Stops or restarts the server |

### Binding actions
| Action | What the backend does |
|---|---|
| `ui.refresh` | Triggers a UI status refresh only |
| `service.start` | Runs the `service.start` command from `game_adapters.json` |
| `service.stop` | Runs the `service.stop` command |
| `service.restart` | Runs stop then start in sequence |
| `backup.create` | Runs the `backup.create` command |
| `property.set` | Writes a key to `server.properties` (Minecraft-style; only if `propertyTypes` is defined for the game) |

---

## Step 3 — Add the adapter entry

In `game_adapters.json`, under `"games"`, add:

```json
"my-game-id": {
  "projectDir": "my-game-server",
  "commands": {
    "service.start": [["start.sh", 120]],
    "service.stop": [["stop.sh", 60]],
    "service.restart": [["stop.sh", 60], ["start.sh", 120]]
  },
  "propertyTypes": {},
  "statusCollector": "process_only",
  "processSearch": "game_server_binary",
  "defaultPort": 2456,
  "portProtocol": "udp"
}
```

### statusCollector options
| Value | What it does |
|---|---|
| `minecraft_ping` | Uses Minecraft server list ping protocol (TCP, reads JSON response) |
| `steam_query` | Uses Steam A2S_INFO query (UDP, reads server name/players/map). Port = defaultPort + 1 for most Source games. |
| `process_only` | Only checks if the process is running via `/proc` — no game-specific query |

### propertyTypes
If your game uses a `server.properties`-style key=value file, list the mutable keys:

```json
"propertyTypes": {
  "server-name": "string",
  "max-players": "integer",
  "pvp": "boolean"
}
```

Then your profile can include controls with `"binding": { "action": "property.set", "key": "max-players" }`.

---

## Quick reference: Popular games

| Game | Steam AppID | Anon Login | Status | Rough RAM |
|---|---|---|---|---|
| Valheim | 896660 | Yes | steam_query | 2-4 GB |
| CS2 | 730 | No (needs GSLT) | steam_query | 1-4 GB |
| Don't Starve Together | 343050 | Yes | process_only | 1-2 GB |
| Satisfactory | 1690800 | Yes | process_only | 8-16 GB |
| Enshrouded | 2278520 | Yes | steam_query | 8-16 GB |
| Sons of the Forest | 2465200 | Yes | steam_query | 4-8 GB |
| 7 Days to Die | 294420 | No | steam_query | 4-8 GB |
| Terraria | N/A | N/A | process_only | 0.5-2 GB |

---

## Verification

After adding a game, restart the console and verify:

```bash
cd "/run/media/zim/a drive/Hermes/Projects/game-host-dashboard"
./stop.sh && ./start.sh
curl -s http://127.0.0.1:5057/api/controls | python3 -m json.tool | grep -A2 '"id"'
```

The new game should appear in the catalog. If the server isn't actually running yet, the profile will still render with the server marked offline and start/stop controls functional.
