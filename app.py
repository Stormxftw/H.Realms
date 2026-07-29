#!/usr/bin/env python3
from __future__ import annotations
import argparse, ipaddress, json, os, re, shutil, socket, struct, subprocess, time, urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

from control_engine import ControlEngine, ControlEngineError

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
PROJECTS_ROOT = Path(os.environ.get("HERMES_PROJECTS_ROOT", str(ROOT.parent))).expanduser().resolve()
PROFILES_DIR = Path(os.environ.get("GAME_HOST_PROFILES_DIR", str(ROOT / "game_profiles"))).expanduser().resolve()
ADAPTER_CONFIG_PATH = Path(os.environ.get("GAME_HOST_ADAPTER_CONFIG", str(ROOT / "game_adapters.json"))).expanduser().resolve()
AUDIT_PATH = Path(os.environ.get("GAME_HOST_AUDIT_PATH", str(ROOT / "data" / "control-audit.jsonl"))).expanduser().resolve()
MINECRAFT_DIR = PROJECTS_ROOT / "minecraft-server"
PALWORLD_DIR = PROJECTS_ROOT / "palworld-server-local"
DEFAULT_PORT = int(os.environ.get("DASHBOARD_PORT", "5057"))
DEFAULT_HOST = "127.0.0.1"
LOCAL_ACTOR = "local-console"
BRIDGE_ACTOR = "hermes-authenticated-bridge"
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

def find_process_pid(needle: str, *, proc_root: Path = Path("/proc")) -> int | None:
    if not needle:
        return None
    try:
        candidates = sorted(
            (entry for entry in proc_root.iterdir() if entry.name.isdigit()),
            key=lambda entry: int(entry.name),
        )
    except OSError:
        return None
    for entry in candidates:
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode(
                "utf-8", errors="replace"
            )
        except OSError:
            continue
        if needle in command:
            return int(entry.name)
    return None


def find_minecraft_pid() -> int | None:
    return find_process_pid("server.jar nogui")

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
                "connect":{"public":"108.91.89.116:25565","lan":"10.0.0.2:25565","local":"127.0.0.1:25565"},
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
        pid = find_process_pid("PalServer-Linux-Shipping")
        process = proc_stats(pid)
        sockets = run(["ss", "-lunp"], timeout=3)
        socket_text = sockets.get("stdout", "")
        listeners = [
            {"port": port, "protocol": "udp", "listening": f":{port} " in socket_text}
            for port in (8211, 27015)
        ]
        ip = public_ip()
        return {
            "name": "Palworld",
            "displayName": "Palworld",
            "online": bool(process.get("running")),
            "connect": {
                "public": f"{ip or 'unavailable'}:8211",
                "lan": "10.0.0.2:8211",
                "local": "127.0.0.1:8211",
            },
            "process": process,
            "listeners": listeners,
            "backup": newest([PALWORLD_DIR / "backups" / "*.zip"]),
            "collector": {
                "ok": True,
                "source": "linux-procfs",
                "socketProbeOk": bool(sockets.get("ok")),
            },
        }
    return cached("palworld", 10, collect)

# ---------------------------------------------------------------------------
# Generic game server status — reads adapter config to detect + report
# ---------------------------------------------------------------------------

def _load_adapter_config() -> dict[str, Any]:
    """Load the game adapter config for status collectors."""
    if not ADAPTER_CONFIG_PATH.is_file():
        return {}
    try:
        data = json.loads(ADAPTER_CONFIG_PATH.read_text(encoding="utf-8"))
        return data.get("games", {})
    except Exception:
        return {}

def _adapter_config() -> dict[str, Any]:
    """Cached adapter config. Reloads on cache miss only."""
    if "_adapters" not in CACHE or time.time() - CACHE["_adapters"][0] > 120:
        CACHE["_adapters"] = (time.time(), _load_adapter_config())
    return CACHE["_adapters"][1]

def generic_server_status(game_id: str) -> dict[str, Any]:
    """Collect server status for any game in the adapter config.

    Uses the adapter's statusCollector field to decide how to probe:
    - minecraft_ping: MC server list protocol
    - steam_query: A2S_INFO UDP query
    - process_only: /proc check only
    """
    def collect():
        adapters = _adapter_config()
        adapter = adapters.get(game_id, {})
        if not adapter:
            return {"name": game_id, "online": False, "error": "no adapter config"}

        name = adapter.get("displayName", game_id)
        # Try to read display name from profile
        try:
            profile_path = PROFILES_DIR / f"{game_id}.json"
            if profile_path.is_file():
                profile = json.loads(profile_path.read_text(encoding="utf-8"))
                name = profile.get("name", name)
        except Exception:
            pass
        proc_search = adapter.get("processSearch", "")
        default_port = adapter.get("defaultPort", 0)
        protocol = adapter.get("portProtocol", "udp")
        collector = adapter.get("statusCollector", "process_only")

        pid = find_process_pid(proc_search) if proc_search else None
        process = proc_stats(pid)
        online = bool(process.get("running"))
        ip = public_ip()

        listeners = []
        if default_port:
            sockets = run(["ss", "-lunp"], timeout=3)
            socket_text = sockets.get("stdout", "")
            listeners = [
                {"port": default_port, "protocol": protocol, "listening": f":{default_port} " in socket_text}
            ]

        result: dict[str, Any] = {
            "name": name,
            "displayName": name,
            "online": online,
            "connect": {
                "public": f"{ip or 'unavailable'}:{default_port}",
                "lan": f"10.0.0.2:{default_port}",
                "local": f"127.0.0.1:{default_port}",
            },
            "process": process,
            "listeners": listeners,
            "collector": {
                "ok": True,
                "source": collector,
                "processSearch": proc_search,
            },
        }

        # Minecraft-specific ping
        if collector == "minecraft_ping" and online and default_port:
            try:
                ping = minecraft_ping(port=default_port)
                players = ping.get("players") or {}
                sample = players.get("sample") or []
                result["version"] = (ping.get("version") or {}).get("name")
                result["players"] = {
                    "online": players.get("online", 0),
                    "max": players.get("max", 0),
                    "names": [p.get("name") for p in sample if p.get("name")],
                }
            except Exception as exc:
                result["pingError"] = str(exc)

        # Steam A2S_INFO query
        if collector == "steam_query" and online and default_port:
            try:
                steam_info = steam_a2s_info("127.0.0.1", default_port)
                if steam_info:
                    result["serverName"] = steam_info.get("name", "")
                    result["map"] = steam_info.get("map", "")
                    result["players"] = {
                        "online": steam_info.get("players", 0),
                        "max": steam_info.get("max_players", 0),
                    }
            except Exception as exc:
                result["steamQueryError"] = str(exc)

        return result

    return cached(f"generic_{game_id}", 10, collect)


def steam_a2s_info(host: str = "127.0.0.1", port: int = 27015, timeout: float = 3.0) -> dict[str, Any] | None:
    """Send a Steam A2S_INFO query and return parsed server info.

    Uses the standard Source engine query protocol:
    - 4 bytes: 0xFF 0xFF 0xFF 0xFF (header)
    - 1 byte: 'T' (A2S_INFO request)
    - payload: "Source Engine Query\0"
    Returns None if the server doesn't respond or isn't a Source engine server.
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        # A2S_INFO challenge-free request
        request = b'\xff\xff\xff\xffTSource Engine Query\x00'
        sock.sendto(request, (host, port))
        data, _ = sock.recvfrom(1400)
        sock.close()

        if len(data) < 6 or data[:4] != b'\xff\xff\xff\xff':
            return None

        # Parse response
        offset = 5  # skip 0xFF x4 + type 'I'
        result: dict[str, Any] = {}

        # Protocol version
        result["protocol"] = data[offset]
        offset += 1

        # Server name (null-terminated)
        end = data.find(b'\x00', offset)
        result["name"] = data[offset:end].decode("utf-8", errors="replace")
        offset = end + 1

        # Map (null-terminated)
        end = data.find(b'\x00', offset)
        result["map"] = data[offset:end].decode("utf-8", errors="replace")
        offset = end + 1

        # Folder (null-terminated)
        end = data.find(b'\x00', offset)
        result["folder"] = data[offset:end].decode("utf-8", errors="replace")
        offset = end + 1

        # Game (null-terminated)
        end = data.find(b'\x00', offset)
        result["game"] = data[offset:end].decode("utf-8", errors="replace")
        offset = end + 1

        # ID (2 bytes, little endian — may not exist in newer responses)
        if offset + 1 < len(data):
            result["id"] = data[offset] | (data[offset + 1] << 8)
        offset += 2

        # Players and max players (1 byte each)
        if offset < len(data):
            result["players"] = data[offset]
        if offset + 1 < len(data):
            result["max_players"] = data[offset + 1]

        return result
    except Exception:
        return None

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
    adapters = _adapter_config()
    services: dict[str, Any] = {}
    for game_id, adapter in adapters.items():
        collector = adapter.get("statusCollector", "process_only")
        if collector == "minecraft_ping":
            services[game_id] = minecraft_status()
        else:
            services[game_id] = generic_server_status(game_id)
    data = {"generatedAt": iso(), "readOnly": True, "refreshSeconds": 5, "services": services, "host": host_status()}
    data["collectorMs"] = round((time.time()-started)*1000)
    return data

class Handler(BaseHTTPRequestHandler):
    server_version = "HermesGameHostConsole/0.2"

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[{iso()}] {self.address_string()} {format % args}", flush=True)

    @property
    def control_engine(self) -> ControlEngine:
        return self.server.control_engine  # type: ignore[attr-defined]

    def send_json(self, code: int, obj: Any) -> None:
        raw = json.dumps(obj, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def send_file(self, path: Path, ctype: str) -> None:
        raw = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 65_536:
            raise ControlEngineError("invalid request body size")
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise ControlEngineError("request body must be a JSON object")
        return value

    def reject_untrusted_host(self) -> bool:
        host_headers = self.headers.get_all("Host", [])
        server_address = self.server.server_address
        valid = False
        if (
            isinstance(server_address, tuple)
            and len(server_address) >= 2
            and len(host_headers) == 1
        ):
            valid = accepted_request_host(
                host_headers[0], str(server_address[0]), int(server_address[1])
            )
        if not valid:
            self.send_json(400, {"error": "invalid Host header"})
            return True
        return False

    def do_GET(self) -> None:
        if self.reject_untrusted_host():
            return
        try:
            if self.path in ("/", "/index.html"):
                self.send_file(STATIC / "index.html", "text/html; charset=utf-8")
            elif self.path == "/health":
                self.send_json(
                    200,
                    {
                        "ok": True,
                        "service": "hermes-game-host-console",
                        "controlMode": "preview-confirm-audit",
                        "generatedAt": iso(),
                    },
                )
            elif self.path == "/api/status":
                self.send_json(200, dashboard_data())
            elif self.path == "/api/controls":
                self.send_json(200, self.control_engine.catalog())
            elif self.path == "/static/app.css":
                self.send_file(STATIC / "app.css", "text/css; charset=utf-8")
            elif self.path == "/static/app.js":
                self.send_file(STATIC / "app.js", "application/javascript; charset=utf-8")
            else:
                self.send_json(404, {"error": "not found"})
        except ControlEngineError as exc:
            self.send_json(400, {"error": str(exc), "generatedAt": iso()})
        except Exception as exc:
            self.send_json(500, {"error": str(exc), "generatedAt": iso()})

    def do_POST(self) -> None:
        if self.reject_untrusted_host():
            return
        try:
            body = self.read_json()
            plan_actors = {
                "/api/control/plan": LOCAL_ACTOR,
                "/api/bridge/control/plan": BRIDGE_ACTOR,
            }
            apply_actors = {
                "/api/control/apply": LOCAL_ACTOR,
                "/api/bridge/control/apply": BRIDGE_ACTOR,
            }
            if self.path in plan_actors:
                result = self.control_engine.plan(
                    game_id=str(body.get("gameId", "")),
                    control_id=str(body.get("controlId", "")),
                    value=body.get("value"),
                    actor=plan_actors[self.path],
                )
                self.send_json(200, result)
            elif self.path in apply_actors:
                digest = body.get("planDigest")
                if not isinstance(digest, str) or not digest.strip():
                    raise ControlEngineError("planDigest is required")
                result = self.control_engine.apply(
                    plan_id=str(body.get("planId", "")),
                    actor=apply_actors[self.path],
                    confirmed=body.get("confirmed") is True,
                    plan_digest=digest,
                )
                self.send_json(200, result)
            else:
                self.send_json(404, {"error": "not found"})
        except (ControlEngineError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": str(exc), "generatedAt": iso()})
        except Exception as exc:
            self.send_json(500, {"error": str(exc), "generatedAt": iso()})


def accepted_request_host(host_header: str, bound_host: str, bound_port: int) -> bool:
    allowed_hosts = {bound_host.lower()}
    if bound_host == "127.0.0.1":
        allowed_hosts.add("localhost")
    allowed_pattern = "|".join(re.escape(value) for value in sorted(allowed_hosts))
    match = re.fullmatch(
        rf"({allowed_pattern})(?::([0-9]{{1,5}}))?",
        host_header,
        re.IGNORECASE,
    )
    if match is None:
        return False
    declared_port = match.group(2)
    return declared_port is None or declared_port == str(bound_port)


def require_loopback_host(host: str) -> str:
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("host must be an IPv4 loopback address") from exc
    if address.version != 4 or not address.is_loopback:
        raise ValueError("host must be an IPv4 loopback address")
    return host


def create_server(
    *,
    host: str,
    port: int,
    projects_root: Path = PROJECTS_ROOT,
    profiles_dir: Path = PROFILES_DIR,
    audit_path: Path = AUDIT_PATH,
    adapter_config_path: Path | None = None,
) -> ThreadingHTTPServer:
    require_loopback_host(host)
    cfg_path = adapter_config_path or ADAPTER_CONFIG_PATH
    server = ThreadingHTTPServer((host, port), Handler)
    server.control_engine = ControlEngine(  # type: ignore[attr-defined]
        projects_root=projects_root,
        profiles_dir=profiles_dir,
        audit_path=audit_path,
        adapter_config_path=cfg_path,
    )
    return server


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    httpd = create_server(host=args.host, port=args.port)
    print(f"Game host dashboard listening on http://{args.host}:{args.port}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
