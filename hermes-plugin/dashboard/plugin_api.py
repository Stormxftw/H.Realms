"""Authenticated Hermes dashboard bridge for Game Host Console.

Mounted by Hermes at /api/plugins/game-host-console. It serves the console UI
inside Hermes Desktop and proxies only an explicit set of typed local endpoints.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import NamedTuple

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool

router = APIRouter()

_LOG = logging.getLogger("hermes-plugin.game-host-console")

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
ART_DIR = next(
    (
        candidate
        for candidate in (ROOT / "art", ROOT.parents[1] / "assets" / "game-art")
        if (candidate / "manifest.json").is_file()
    ),
    ROOT / "art",
)
PLUGIN_BASE = "/api/plugins/game-host-console"
MAX_BODY = 65_536
MAX_RESPONSE = 1_048_576
MAX_ART_BYTES = 512_000
MAX_ART_RESPONSE = 768_000
MAX_ART_MANIFEST_BYTES = 65_536


class ProxyRule(NamedTuple):
    upstream_path: str
    max_body: int
    max_response: int


PROXY_RULES = {
    ("GET", "health"): ProxyRule("health", 0, 16_384),
    ("GET", "api/status"): ProxyRule("api/status", 0, MAX_RESPONSE),
    ("GET", "api/controls"): ProxyRule("api/controls", 0, MAX_RESPONSE),
    ("GET", "api/store"): ProxyRule("api/store", 0, MAX_RESPONSE),
    ("GET", "api/operations"): ProxyRule("api/operations", 0, 524_288),

    ("POST", "api/control/plan"): ProxyRule("api/bridge/control/plan", MAX_BODY, 262_144),
    ("POST", "api/control/apply"): ProxyRule("api/bridge/control/apply", MAX_BODY, 262_144),
    ("POST", "api/store/install"): ProxyRule("api/bridge/store/install", MAX_BODY, 262_144),
    ("POST", "api/store/uninstall"): ProxyRule("api/bridge/store/uninstall", MAX_BODY, 262_144),
}
DYNAMIC_PROXY_RULES = (
    ("GET", re.compile(r"api/operations/[A-Za-z0-9][A-Za-z0-9_-]{0,127}"), 0, 524_288),
    ("GET", re.compile(r"api/backups/[a-z0-9][a-z0-9-]{0,63}"), 0, MAX_RESPONSE),
    ("POST", re.compile(r"api/backups/[a-z0-9][a-z0-9-]{0,63}/create"), MAX_BODY, 262_144),
    ("POST", re.compile(r"api/backups/[a-z0-9][a-z0-9-]{0,63}/restore/preview"), MAX_BODY, 262_144),
    ("POST", re.compile(r"api/backups/[a-z0-9][a-z0-9-]{0,63}/restore"), MAX_BODY, MAX_RESPONSE),
    ("GET", re.compile(r"api/diagnostics/[a-z0-9][a-z0-9-]{0,63}/logs"), 0, 262_144),
    (
        "GET",
        re.compile(r"api/diagnostics/[a-z0-9][a-z0-9-]{0,63}/logs/[A-Za-z0-9][A-Za-z0-9_-]{0,63}"),
        0,
        MAX_RESPONSE,
    ),
    ("GET", re.compile(r"api/diagnostics/[a-z0-9][a-z0-9-]{0,63}/bundle"), 0, MAX_RESPONSE),
)


def _validate_service_base(value: str) -> str:
    error = (
        "GAME_HOST_SERVICE_URL must be a plain http origin using a loopback "
        "IPv4 literal with no credentials, path, query, or fragment"
    )
    if not value or value != value.strip():
        raise ValueError(error)
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
        address = ipaddress.ip_address(parsed.hostname or "")
    except ValueError as exc:
        raise ValueError(error) from exc
    if (
        parsed.scheme != "http"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or port == 0
        or address.version != 4
        or not address.is_loopback
    ):
        raise ValueError(error)
    expected_netloc = address.compressed
    if port is not None:
        expected_netloc = f"{expected_netloc}:{port}"
    if parsed.netloc != expected_netloc:
        raise ValueError(error)
    return f"http://{expected_netloc}"


SERVICE_BASE = _validate_service_base(
    os.environ.get("GAME_HOST_SERVICE_URL", "http://127.0.0.1:5057")
)


def build_app_html(source: str) -> str:
    """Rebase the standalone app to authenticated, same-origin plugin routes."""
    source = source.replace(
        '<html lang="en">',
        f'<html lang="en" data-api-base="{PLUGIN_BASE}/proxy" data-embedded="true">',
        1,
    )
    source = source.replace('href="/static/app.css"', f'href="{PLUGIN_BASE}/assets/app.css"')
    source = source.replace('src="/static/app.js"', f'src="{PLUGIN_BASE}/assets/app.js"')
    return source


def _read_web(name: str) -> bytes:
    path = WEB_DIR / name
    if not path.is_file():
        raise HTTPException(status_code=503, detail=f"Game Host Console asset is not installed: {name}")
    return path.read_bytes()


def _game_art_entry(game_id: str) -> dict[str, object]:
    if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", game_id) is None:
        raise HTTPException(status_code=404, detail="Game artwork is unavailable")
    manifest_path = ART_DIR / "manifest.json"
    try:
        if manifest_path.stat().st_size > MAX_ART_MANIFEST_BYTES:
            raise ValueError("art manifest exceeds safety limit")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assets = manifest.get("assets")
        if manifest.get("schemaVersion") != 1 or not isinstance(assets, list):
            raise ValueError("invalid art manifest")
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        _LOG.exception("packaged game-art manifest is unavailable or invalid: %s", exc)
        raise HTTPException(status_code=503, detail="Packaged game artwork is unavailable") from exc

    entry = next(
        (item for item in assets if isinstance(item, dict) and item.get("gameId") == game_id),
        None,
    )
    if entry is None or entry.get("repoPackagingRecommendation") != "allow":
        raise HTTPException(status_code=404, detail="Game artwork is unavailable")
    return entry


def _read_game_art(entry: dict[str, object]) -> bytes:
    relative_name = entry.get("file")
    if not isinstance(relative_name, str):
        raise HTTPException(status_code=503, detail="Packaged game artwork is invalid")
    relative_path = Path(relative_name)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise HTTPException(status_code=503, detail="Packaged game artwork is invalid")
    try:
        root = ART_DIR.resolve(strict=True)
        path = (root / relative_path).resolve(strict=True)
        path.relative_to(root)
        size = path.stat().st_size
        expected_size = entry.get("sizeBytes")
        expected_digest = entry.get("sha256")
        if (
            not isinstance(expected_size, int)
            or size != expected_size
            or size > MAX_ART_BYTES
            or entry.get("mediaType") != "image/webp"
            or not isinstance(expected_digest, str)
        ):
            raise ValueError("packaged art metadata mismatch")
        content = path.read_bytes()
        if content[:4] != b"RIFF" or content[8:12] != b"WEBP":
            raise ValueError("packaged art is not WebP")
        if hashlib.sha256(content).hexdigest() != expected_digest:
            raise ValueError("packaged art digest mismatch")
        return content
    except (OSError, ValueError) as exc:
        _LOG.exception("packaged game-art file failed validation: %s", exc)
        raise HTTPException(status_code=503, detail="Packaged game artwork is invalid") from exc


def _proxy_rule(method: str, path: str) -> ProxyRule:
    rule = PROXY_RULES.get((method, path))
    if rule is not None:
        return rule
    for allowed_method, pattern, max_body, max_response in DYNAMIC_PROXY_RULES:
        if method == allowed_method and pattern.fullmatch(path):
            return ProxyRule(path, max_body, max_response)
    raise HTTPException(status_code=404, detail="Proxy route is not allowed")


def _read_upstream(upstream, limit: int) -> bytes:
    content = upstream.read(limit + 1)
    if len(content) > limit:
        raise HTTPException(status_code=502, detail="Upstream response exceeded safety limit")
    return content


async def _read_request_body(request: Request, limit: int) -> bytes:
    declared_length = request.headers.get("content-length")
    if declared_length is not None:
        try:
            length = int(declared_length)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length") from exc
        if length < 0:
            raise HTTPException(status_code=400, detail="Invalid Content-Length")
        if length > limit:
            raise HTTPException(status_code=413, detail="Request body exceeded safety limit")

    content = bytearray()
    async for chunk in request.stream():
        if len(chunk) > limit - len(content):
            raise HTTPException(status_code=413, detail="Request body exceeded safety limit")
        content.extend(chunk)
    return bytes(content)


def _validated_query(path: str, query: dict[str, object] | None) -> str:
    if not query:
        return ""
    diagnostics_log = re.fullmatch(
        r"api/diagnostics/[a-z0-9][a-z0-9-]{0,63}/logs/[A-Za-z0-9][A-Za-z0-9_-]{0,63}",
        path,
    )
    if diagnostics_log is not None:
        redact = query.get("redact")
        if set(query) != {"redact"} or not isinstance(redact, str) or redact not in {"true", "false"}:
            raise HTTPException(status_code=400, detail="Invalid proxy query")
        return "?" + urllib.parse.urlencode({"redact": redact})
    if path != "api/operations" or set(query) - {"limit", "gameId", "state"}:
        raise HTTPException(status_code=400, detail="Invalid proxy query")
    normalized: dict[str, str] = {}
    for key, raw in query.items():
        if not isinstance(raw, str):
            raise HTTPException(status_code=400, detail="Invalid proxy query")
        normalized[key] = raw
    if "limit" in normalized:
        try:
            limit = int(normalized["limit"])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid proxy query") from exc
        if not 1 <= limit <= 500:
            raise HTTPException(status_code=400, detail="Invalid proxy query")
    if "gameId" in normalized and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", normalized["gameId"]):
        raise HTTPException(status_code=400, detail="Invalid proxy query")
    if "state" in normalized and normalized["state"] not in {
        "queued", "running", "succeeded", "failed", "cancelled", "outcome_unknown"
    }:
        raise HTTPException(status_code=400, detail="Invalid proxy query")
    return "?" + urllib.parse.urlencode(normalized)


def _proxy(
    method: str,
    path: str,
    body: bytes | None = None,
    *,
    query: dict[str, object] | None = None,
) -> Response:
    rule = _proxy_rule(method, path)
    if body is not None and len(body) > rule.max_body:
        raise HTTPException(status_code=413, detail="Request body exceeded safety limit")
    request = urllib.request.Request(
        f"{SERVICE_BASE}/{rule.upstream_path}{_validated_query(path, query)}",
        data=body,
        method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=320) as upstream:
            content = _read_upstream(upstream, rule.max_response)
            return Response(
                content=content,
                status_code=upstream.status,
                media_type="application/json",
                headers={"Cache-Control": "no-store"},
            )
    except urllib.error.HTTPError as exc:
        content = _read_upstream(exc, rule.max_response)
        return Response(
            content=content,
            status_code=exc.code,
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )
    except urllib.error.URLError as exc:
        # Never surface local connection details (addresses, socket errors,
        # filesystem paths) to the authenticated client. Log server-side only.
        _LOG.exception("upstream Game Host Console connection failed for %s: %s", path, exc)
        raise HTTPException(
            status_code=503,
            detail="Local Game Host Console is unavailable.",
        ) from exc


@router.get("/art/{game_id}", response_class=JSONResponse)
def game_art(game_id: str) -> JSONResponse:
    entry = _game_art_entry(game_id)
    content = _read_game_art(entry)
    response = JSONResponse(
        {
            "gameId": game_id,
            "mediaType": "image/webp",
            "dataUrl": "data:image/webp;base64," + base64.b64encode(content).decode("ascii"),
            "objectPosition": entry.get("objectPosition", "50% 50%"),
            "attribution": entry.get("attribution", "Packaged game artwork"),
            "licenseSpdx": entry.get("licenseSpdx"),
        },
        headers={"Cache-Control": "private, max-age=86400"},
    )
    if len(response.body) > MAX_ART_RESPONSE:
        raise HTTPException(status_code=503, detail="Packaged game artwork exceeded safety limit")
    return response


@router.get("/app", response_class=HTMLResponse)
def app_page() -> HTMLResponse:
    source = _read_web("index.html").decode("utf-8")
    return HTMLResponse(build_app_html(source), headers={"Cache-Control": "no-store"})


@router.get("/assets/app.css")
def app_css() -> Response:
    return Response(_read_web("app.css"), media_type="text/css", headers={"Cache-Control": "no-store"})


@router.get("/assets/app.js")
def app_js() -> Response:
    return Response(
        _read_web("app.js"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/proxy/{path:path}")
async def proxy_get(path: str, request: Request) -> Response:
    rule = _proxy_rule("GET", path)
    await _read_request_body(request, rule.max_body)
    query = urllib.parse.parse_qs(
        request.url.query, keep_blank_values=True, strict_parsing=True
    )
    if any(len(values) != 1 for values in query.values()):
        raise HTTPException(status_code=400, detail="Invalid proxy query")
    flat_query = {key: values[0] for key, values in query.items()}
    return await run_in_threadpool(_proxy, "GET", path, None, query=flat_query)


@router.post("/proxy/{path:path}")
async def proxy_post(path: str, request: Request) -> Response:
    rule = _proxy_rule("POST", path)
    body = await _read_request_body(request, rule.max_body)
    return await run_in_threadpool(_proxy, "POST", path, body)
