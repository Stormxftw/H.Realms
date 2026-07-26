# Hermes Game Host Console Status

- Status: Running — MVP read-only dashboard using the Hermes teal dashboard style.
- Latest verification: `2026-06-28 14:00 EDT` — local health passed after restart; HTML title/header verified as `Hermes Game Host Console`; CSS now mirrors the Hermes Usage Dashboard dark navy/teal/purple palette, compact cards/pills, button hover treatment, and gradient bars; browser visual smoke passed with live Minecraft telemetry.
- Product/add-on name: `Hermes Game Host Console`
- Port: `5057`
- WSL URL: `http://127.0.0.1:5057`
- LAN URL: `http://203.0.113.10:5057`
- Safety: read-only; no Minecraft/Palworld restarts; no control buttons.
- Styling: aligned to Hermes Usage Dashboard palette (`#0b1020`, `#121a32`, `#a78bfa`, `#2dd4bf`, `#25304a`) and layout language.
- Data sources:
  - Minecraft server-list ping on `127.0.0.1:25565`
  - Minecraft process and redacted log pulse from WSL
  - Palworld Windows process/listener checks
  - WSL memory/load/disk
  - Windows memory/disk via PowerShell
  - Backup folder freshness
