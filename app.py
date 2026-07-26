#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, os, re, shutil, socket, struct, subprocess, time, urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DOWNLOADS = ROOT / "downloads"
MINECRAFT_DIR = Path("/mnt/d/Hermes/Projects/minecraft-server")
PALWORLD_DIR = Path("/mnt/d/Hermes/Projects/palworld-server-local")
DEFAULT_PORT = int(os.environ.get("DASHBOARD_PORT", "5057"))
DEFAULT_HOST = os.environ.get("DASHBOARD_HOST", "0.0.0.0")
CACHE: dict[str, tuple[float, Any]] = {}

def iso(epoch: float | None = None) -> str:
    return datetime.fromtimestamp(epoch or time.time(), timezone.utc).astimezone().isoformat(timespec="seconds")

def human_duration(seconds: float) -> str:
    s = int(max(0, seconds)); d, r = divmod(s, 86400); h, r = divmod(r, 3600); m, _ = divmod(r, 60)
    return (f"{d}d " if d else "") + (f"{h}h " if h or d else "") + f"{m}m"

def cached(key: str, ttl: float, fn):
    now = time.time(); hit = CACHE.get(key)
    if hit and now - hit[0] < ttl: return hit[1]
    value = fn(); CACHE[key] = (now, value); return value

def run(cmd: list[str], timeout: int = 6) -> dict[str, Any]:
    started = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {"ok": p.returncode == 0, "exitCode": p.returncode, "stdout": p.stdout.strip(), "stderr": p.stderr.strip(), "durationMs": round((time.time()-started)*1000)}
    except subprocess.TimeoutExpired as e:
        return {"ok": False, "exitCode": None, "stdout": e.stdout or "", "stderr": "timeout", "durationMs": round((time.time()-started)*1000)}
    except Exception as e:
        return {"ok": False, "exitCode": None, "stdout": "", "stderr": str(e), "durationMs": round((time.time()-started)*1000)}

def vi(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7f; n >>= 7
        out.append(b | 0x80 if n else b)
        if not n: return bytes(out)

def rv(sock: socket.socket) -> int:
    n = 0; sh = 0
    for _ in range(5):
        b = sock.recv(1)
        if not b: raise EOFError("socket closed")
        v = b[0]; n |= (v & 0x7f) << sh
        if not (v & 0x80): return n
        sh += 7
    raise ValueError("varint too long")

def pkt(pid: int, payload: bytes = b"") -> bytes:
    body = vi(pid) + payload
    return vi(len(body)) + body

def minecraft_ping(host="127.0.0.1", port=25565) -> dict[str, Any]:
    addr = host.encode(); hs = vi(0) + vi(len(addr)) + addr + struct.pack(">H", port) + vi(1)
    with socket.create_connection((host, port), timeout=3) as s:
        s.sendall(pkt(0, hs)); s.sendall(pkt(0)); rv(s); rv(s); length = rv(s)
        data = b""
        while len(data) < length:
            data += s.recv(length-len(data))
    return json.loads(data.decode())

def find_minecraft_pid() -> int | None:
    out = run(["bash", "-lc", "pgrep -f 'server.jar nogui' | head -n 1"], timeout=3)["stdout"].strip()
    return int(out) if out.isdigit() else None

def proc_stats(pid: int | None) -> dict[str, Any]:
    if not pid: return {"running": False}
    proc = Path(f"/proc/{pid}")
    if not proc.exists(): return {"running": False, "pid": pid}
    data: dict[str, Any] = {"running": True, "pid": pid}
    try:
        for line in (proc/"status").read_text(errors="replace").splitlines():
            if line.startswith("VmRSS:"): data["rssMB"] = round(int(line.split()[1]) / 1024, 1)
            if line.startswith("Threads:"): data["threads"] = int(line.split()[1])
        stat = (proc/"stat").read_text().split(); clk = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
        boot = float(next(x.split()[1] for x in Path("/proc/stat").read_text().splitlines() if x.startswith("btime")))
        start_epoch = boot + int(stat[21]) / clk; uptime = time.time() - start_epoch
        data["uptimeSeconds"] = int(uptime); data["uptimeHuman"] = human_duration(uptime); data["cpuSeconds"] = round((int(stat[13])+int(stat[14]))/clk, 1)
    except Exception: pass
    return data

def redact(line: str) -> str:
    line = re.sub(r"/\d{1,3}(?:\.\d{1,3}){3}:\d+", "/[redacted-ip]", line)
    return re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}:\d+\b", "[redacted-ip]", line)

def log_pulse() -> list[str]:
    path = MINECRAFT_DIR / "logs" / "latest.log"
    if not path.exists(): return []
    try: lines = path.read_text(errors="replace").splitlines()[-100:]
    except Exception: return []
    keys = ("joined the game", "left the game", "lost connection", "logged in", "Done (", "Starting Minecraft", "Server empty")
    return [redact(x) for x in lines if any(k in x for k in keys)][-24:]

def newest(globs: list[Path]) -> dict[str, Any] | None:
    files = []
    for g in globs:
        if g.parent.exists(): files += [p for p in g.parent.glob(g.name) if p.is_file()]
    if not files: return None
    f = max(files, key=lambda p: p.stat().st_mtime); st = f.stat()
    return {"path": str(f), "name": f.name, "sizeMB": round(st.st_size/1024/1024, 2), "mtime": iso(st.st_mtime), "ageSeconds": int(time.time()-st.st_mtime), "ageHuman": human_duration(time.time()-st.st_mtime)}

def minecraft_status() -> dict[str, Any]:
    def collect():
        pid = find_minecraft_pid(); process = proc_stats(pid)
        try:
            ping = minecraft_ping(); online = True; error = None
        except Exception as e:
            ping = {}; online = False; error = str(e)
        players = ping.get("players") or {}; sample = players.get("sample") or []
        return {"name":"Minecraft", "displayName": ping.get("description") or "Minecraft", "online": online, "error": error,
                "connect":{"public":"108.91.89.116:25565","lan":"203.0.113.10:25565","local":"127.0.0.1:25565"},
                "version": (ping.get("version") or {}).get("name"), "protocol": (ping.get("version") or {}).get("protocol"),
                "players":{"online": players.get("online", 0), "max": players.get("max", 0), "names":[p.get("name") for p in sample if p.get("name")]},
                "process": process, "logs": log_pulse(), "backup": newest([MINECRAFT_DIR/"backups"/"world_*.tar.gz"])}
    return cached("minecraft", 5, collect)

def powershell_json(script: str, timeout=8) -> tuple[dict[str, Any], dict[str, Any]]:
    result = run(["powershell.exe", "-NoProfile", "-Command", script], timeout=timeout)
    data: dict[str, Any] = {}
    if result["stdout"]:
        try: data = json.loads(result["stdout"])
        except Exception: data = {}
    return data, {"ok": result["ok"], "durationMs": result["durationMs"], "error": result["stderr"][:200]}

def palworld_status() -> dict[str, Any]:
    def collect():
        ps = "\n".join([
            "$ErrorActionPreference = 'SilentlyContinue'",
            "$procs = Get-Process | Where-Object { $_.ProcessName -like '*PalServer*' } | ForEach-Object { [pscustomobject]@{ Id=$_.Id; Name=$_.ProcessName; CPU=$_.CPU; WorkingSetMB=[math]::Round($_.WorkingSet64/1MB,1); StartTime=$_.StartTime.ToString('s') } }",
            "$udp = Get-NetUDPEndpoint -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -in 8211,27015 } | ForEach-Object { [pscustomobject]@{ LocalAddress=$_.LocalAddress; LocalPort=$_.LocalPort; OwningProcess=$_.OwningProcess } }",
            "[pscustomobject]@{ Processes=@($procs); Udp=@($udp) } | ConvertTo-Json -Depth 5 -Compress"
        ])
        data, meta = powershell_json(ps)
        procs = data.get("Processes") or []; udp = data.get("Udp") or []
        if isinstance(procs, dict): procs = [procs]
        if isinstance(udp, dict): udp = [udp]
        return {"name":"Palworld", "displayName":"Palworld", "online": len(procs)>0,
                "connect":{"public":"108.91.89.116:8211","lan":"203.0.113.10:8211"}, "processes": procs, "listeners": udp,
                "backup": newest([PALWORLD_DIR/"backups"/"*.zip"]), "collector": meta}
    return cached("palworld", 10, collect)

def windows_status() -> dict[str, Any]:
    ps = "\n".join([
        "$ErrorActionPreference = 'SilentlyContinue'",
        "$os = Get-CimInstance Win32_OperatingSystem",
        "$drives = Get-PSDrive -PSProvider FileSystem | ForEach-Object { [pscustomobject]@{ Name=$_.Name; UsedGB=[math]::Round($_.Used/1GB,1); FreeGB=[math]::Round($_.Free/1GB,1) } }",
        "[pscustomobject]@{ Memory=[pscustomobject]@{ FreeGB=[math]::Round($os.FreePhysicalMemory/1MB,2); TotalGB=[math]::Round($os.TotalVisibleMemorySize/1MB,2) }; Drives=@($drives) } | ConvertTo-Json -Depth 4 -Compress"
    ])
    data, meta = powershell_json(ps)
    data["collector"] = meta
    return data

def public_ip() -> str | None:
    def collect():
        try:
            with urllib.request.urlopen("https://api.ipify.org", timeout=5) as r: return r.read().decode().strip()
        except Exception: return None
    return cached("public_ip", 300, collect)

def host_status() -> dict[str, Any]:
    def collect():
        mem = {}
        try:
            for line in Path("/proc/meminfo").read_text().splitlines():
                k, v = line.split(":", 1); mem[k] = int(v.strip().split()[0])
        except Exception: pass
        total = mem.get("MemTotal", 0); avail = mem.get("MemAvailable", 0); used = max(0, total-avail)
        try: load = os.getloadavg()
        except Exception: load = (0,0,0)
        disks = {}
        for label, path in {"wslRoot": Path("/"), "dataD": Path("/mnt/d"), "minecraftWorld": MINECRAFT_DIR/"world"}.items():
            try:
                du = shutil.disk_usage(path); disks[label] = {"path":str(path), "totalGB":round(du.total/1024**3,1), "usedGB":round(du.used/1024**3,1), "freeGB":round(du.free/1024**3,1), "usedPct":round(du.used/du.total*100,1)}
            except Exception: pass
        ip = public_ip()
        return {"wsl":{"load1":round(load[0],2),"load5":round(load[1],2),"load15":round(load[2],2),"memory":{"totalGB":round(total/1024**2,2),"usedGB":round(used/1024**2,2),"availableGB":round(avail/1024**2,2),"usedPct":round((used/total*100) if total else 0,1)},"disks":disks},
                "windows": windows_status(), "network":{"publicIp": ip, "minecraftPublic": f"{ip or '108.91.89.116'}:25565", "palworldPublic": f"{ip or '108.91.89.116'}:8211"}}
    return cached("host", 8, collect)

def dashboard_data() -> dict[str, Any]:
    started = time.time()
    data = {"generatedAt": iso(), "readOnly": True, "refreshSeconds": 5, "services":{"minecraft": minecraft_status(), "palworld": palworld_status()}, "host": host_status()}
    data["collectorMs"] = round((time.time()-started)*1000)
    return data

class Handler(BaseHTTPRequestHandler):
    server_version = "HermesGameHostConsole/0.1"
    def log_message(self, fmt: str, *args: Any) -> None: print(f"[{iso()}] {self.address_string()} {fmt % args}", flush=True)
    def send_json(self, code: int, obj: Any) -> None:
        raw = json.dumps(obj, indent=2).encode(); self.send_response(code); self.send_header("Content-Type","application/json; charset=utf-8"); self.send_header("Cache-Control","no-store"); self.send_header("Content-Length",str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def send_file(self, path: Path, ctype: str) -> None:
        raw = path.read_bytes(); self.send_response(200); self.send_header("Content-Type", ctype); self.send_header("Cache-Control","no-store"); self.send_header("Content-Length",str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_GET(self) -> None:
        try:
            if self.path in ("/", "/index.html"): self.send_file(STATIC/"index.html", "text/html; charset=utf-8")
            elif self.path == "/health": self.send_json(200, {"ok": True, "service":"hermes-game-host-console", "readOnly": True, "generatedAt": iso()})
            elif self.path == "/api/status": self.send_json(200, dashboard_data())
            elif self.path == "/project-architecture-inventory.zip": self.send_file(DOWNLOADS/"project-architecture-inventory.zip", "application/zip")
            elif self.path == "/static/app.css": self.send_file(STATIC/"app.css", "text/css; charset=utf-8")
            elif self.path == "/static/app.js": self.send_file(STATIC/"app.js", "application/javascript; charset=utf-8")
            else: self.send_json(404, {"error":"not found"})
        except Exception as e: self.send_json(500, {"error": str(e), "generatedAt": iso()})

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--host", default=DEFAULT_HOST); ap.add_argument("--port", type=int, default=DEFAULT_PORT); args = ap.parse_args()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Game host dashboard listening on http://{args.host}:{args.port}", flush=True)
    httpd.serve_forever()
if __name__ == "__main__": main()
