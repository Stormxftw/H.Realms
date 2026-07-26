# Hermes Game Host Console

Read-only local add-on dashboard for hosted game servers.

## Product positioning

Name: **Hermes Game Host Console**

Intended framing: an optional Hermes Agent add-on for local game-host telemetry. It is generic enough to ship with the product without Joe-specific branding, while still matching the Hermes dashboard visual language.

## Safety

- Does not restart Minecraft.
- Does not restart Palworld.
- Does not expose RCON.
- Does not perform writes to game server files.
- Collects status by Minecraft server ping, process/listener checks, backup folder checks, and lightweight host metrics.

## URLs

- WSL/local: `http://127.0.0.1:5057`
- LAN/Windows host: `http://10.0.0.2:5057`

## Commands

```bash
cd /mnt/d/Hermes/Projects/game-host-dashboard
./start.sh
./status.sh
./stop.sh
```

## LAN bridge refresh

If WSL restarts and the LAN URL breaks, run as admin:

```text
D:\Hermes\Projects\game-host-dashboard\RUN-AS-ADMIN-Enable-GameHostDashboard-LAN.cmd
```

## Future ideas

- Auth/token before any control actions.
- RCON-only local collector for TPS and exact Minecraft commands.
- Historical charts in SQLite.
- Start/stop/backup buttons gated behind local auth.
