"""Authenticated Hermes dashboard bridge for Game Host Console.

Mounted by Hermes at /api/plugins/game-host-console. It serves the console UI
inside Hermes Desktop and proxies only an explicit set of typed local endpoints.
"""

from __future__ import annotations

import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import NamedTuple

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

router = APIRouter()

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
SERVICE_BASE = os.environ.get("GAME_HOST_SERVICE_URL", "http://127.0.0.1:5057").rstrip("/")
PLUGIN_BASE = "/api/plugins/game-host-console"
MAX_BODY = 65_536
MAX_RESPONSE = 1_048_576


class ProxyRule(NamedTuple):
    upstream_path: str
    max_body: int
    max_response: int


PROXY_RULES = {
    ("GET", "health"): ProxyRule("health", 0, 16_384),
    ("GET", "api/status"): ProxyRule("api/status", 0, MAX_RESPONSE),
    ("GET", "api/controls"): ProxyRule("api/controls", 0, MAX_RESPONSE),
    ("GET", "api/operations"): ProxyRule("api/operations", 0, 524_288),
    ("GET", "api/diagnostics"): ProxyRule("api/diagnostics", 0, MAX_RESPONSE),
    ("POST", "api/control/plan"): ProxyRule("api/bridge/control/plan", MAX_BODY, 262_144),
    ("POST", "api/control/apply"): ProxyRule("api/bridge/control/apply", MAX_BODY, 262_144),
}
DYNAMIC_PROXY_RULES = (
    ("GET", re.compile(r"api/operations/[A-Za-z0-9][A-Za-z0-9_-]{0,127}"), 524_288),
    ("GET", re.compile(r"api/diagnostics/[a-z0-9][a-z0-9-]{0,63}"), MAX_RESPONSE),
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


def _proxy_rule(method: str, path: str) -> ProxyRule:
    rule = PROXY_RULES.get((method, path))
    if rule is not None:
        return rule
    for allowed_method, pattern, max_response in DYNAMIC_PROXY_RULES:
        if method == allowed_method and pattern.fullmatch(path):
            return ProxyRule(path, 0, max_response)
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


def _proxy(method: str, path: str, body: bytes | None = None) -> Response:
    rule = _proxy_rule(method, path)
    if body is not None and len(body) > rule.max_body:
        raise HTTPException(status_code=413, detail="Request body exceeded safety limit")
    request = urllib.request.Request(
        f"{SERVICE_BASE}/{rule.upstream_path}",
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
        raise HTTPException(
            status_code=503,
            detail=f"Local Game Host Console is unavailable: {exc.reason}",
        ) from exc


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
def proxy_get(path: str) -> Response:
    return _proxy("GET", path)


@router.post("/proxy/{path:path}")
async def proxy_post(path: str, request: Request) -> Response:
    rule = _proxy_rule("POST", path)
    body = await _read_request_body(request, rule.max_body)
    return _proxy("POST", path, body)
