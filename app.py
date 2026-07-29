#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import socket
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

import telemetry
from control_engine import ControlEngine, ControlEngineError

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
PROJECTS_ROOT = Path(
    os.environ.get("HERMES_PROJECTS_ROOT", str(ROOT.parent))
).expanduser().resolve()
PROFILES_DIR = Path(
    os.environ.get("GAME_HOST_PROFILES_DIR", str(ROOT / "game_profiles"))
).expanduser().resolve()
ADAPTER_CONFIG_PATH = Path(
    os.environ.get("GAME_HOST_ADAPTER_CONFIG", str(ROOT / "game_adapters.json"))
).expanduser().resolve()
AUDIT_PATH = Path(
    os.environ.get(
        "GAME_HOST_AUDIT_PATH", str(ROOT / "data" / "control-audit.jsonl")
    )
).expanduser().resolve()
DEFAULT_PORT = int(os.environ.get("DASHBOARD_PORT", "5057"))
DEFAULT_HOST = "127.0.0.1"
LOCAL_ACTOR = "local-console"
BRIDGE_ACTOR = "hermes-authenticated-bridge"
CONTROL_POLICY = "preview-confirm-audit"


def iso(epoch: float | None = None) -> str:
    return datetime.fromtimestamp(
        epoch or time.time(), timezone.utc
    ).astimezone().isoformat(timespec="seconds")


def control_policy_fields() -> dict[str, Any]:
    """Describe the mutation API as it is actually exposed by this service."""
    return {
        "readOnly": False,
        "controlPolicy": CONTROL_POLICY,
        "mutationsEnabled": True,
    }


def discover_lan_address() -> str | None:
    """Return an explicitly configured or locally discovered address, never a fallback."""
    configured = os.environ.get("GAME_HOST_LAN_ADDRESS", "").strip()
    candidates: list[str] = [configured] if configured else []
    if not candidates:
        try:
            candidates.extend(
                str(address[4][0])
                for address in socket.getaddrinfo(
                    socket.gethostname(), None, socket.AF_INET, socket.SOCK_DGRAM
                )
            )
        except OSError:
            pass
    for candidate in candidates:
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if (
            address.version == 4
            and not address.is_loopback
            and not address.is_unspecified
            and not address.is_multicast
        ):
            return str(address)
    return None


def load_adapters(path: Path) -> dict[str, dict[str, Any]]:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    games = document.get("games")
    if not isinstance(games, dict):
        raise ControlEngineError("adapter config games must be an object")
    return games


def catalog_data(control_engine: ControlEngine) -> dict[str, Any]:
    return {**control_engine.catalog(), **control_policy_fields()}


def dashboard_data(
    control_engine: ControlEngine,
    adapter_config_path: Path,
    *,
    telemetry_collector: Callable[..., dict[str, Any]] = telemetry.collect_game,
    lan_address: str | None = None,
) -> dict[str, Any]:
    started = time.time()
    catalog = control_engine.catalog()
    adapters = load_adapters(adapter_config_path)
    detected_lan = lan_address if lan_address is not None else discover_lan_address()
    services: dict[str, Any] = {}

    for game in catalog["games"]:
        game_id = str(game["id"])
        readiness = {
            "readiness": game["readiness"],
            "projectPresent": bool(game.get("projectPresent")),
            "blockers": game["blockers"],
            "capabilities": game["capabilities"],
            "capabilityReasons": game["capabilityReasons"],
        }
        try:
            collected = telemetry_collector(
                game_id=game_id,
                name=str(game["name"]),
                adapter=adapters[game_id],
                readiness=readiness,
                lan_address=detected_lan,
            )
        except Exception as exc:
            # A collector boundary failure is unknown, never silently "offline".
            collected = {
                "id": game_id,
                "name": game["name"],
                "state": "unknown",
                "online": False,
                "process": {
                    "ok": False,
                    "running": None,
                    "pid": None,
                    "error": str(exc),
                },
                "listeners": [],
                "query": {
                    "attempted": False,
                    "ok": None,
                    "errorCode": None,
                    "error": None,
                    "data": None,
                },
                "connect": {"local": None, "lan": None, "public": None},
            }
        services[game_id] = {**collected, **readiness}

    return {
        "generatedAt": iso(),
        **control_policy_fields(),
        "refreshSeconds": 5,
        "services": services,
        "collectorMs": round((time.time() - started) * 1000),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "HermesGameHostConsole/0.3"

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

    def send_file(self, path: Path, content_type: str) -> None:
        raw = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
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
                        "controlMode": CONTROL_POLICY,
                        **control_policy_fields(),
                        "generatedAt": iso(),
                    },
                )
            elif self.path == "/api/status":
                self.send_json(
                    200,
                    dashboard_data(
                        self.control_engine,
                        self.server.adapter_config_path,  # type: ignore[attr-defined]
                        telemetry_collector=self.server.telemetry_collector,  # type: ignore[attr-defined]
                        lan_address=self.server.lan_address,  # type: ignore[attr-defined]
                    ),
                )
            elif self.path == "/api/controls":
                self.send_json(200, catalog_data(self.control_engine))
            elif self.path == "/static/app.css":
                self.send_file(STATIC / "app.css", "text/css; charset=utf-8")
            elif self.path == "/static/app.js":
                self.send_file(
                    STATIC / "app.js", "application/javascript; charset=utf-8"
                )
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
    telemetry_collector: Callable[..., dict[str, Any]] = telemetry.collect_game,
    lan_address: str | None = None,
) -> ThreadingHTTPServer:
    require_loopback_host(host)
    config_path = Path(adapter_config_path or ADAPTER_CONFIG_PATH)
    control_engine = ControlEngine(
        projects_root=projects_root,
        profiles_dir=profiles_dir,
        audit_path=audit_path,
        adapter_config_path=config_path,
    )
    server = ThreadingHTTPServer((host, port), Handler)
    server.control_engine = control_engine  # type: ignore[attr-defined]
    server.adapter_config_path = config_path  # type: ignore[attr-defined]
    server.telemetry_collector = telemetry_collector  # type: ignore[attr-defined]
    server.lan_address = lan_address  # type: ignore[attr-defined]
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
