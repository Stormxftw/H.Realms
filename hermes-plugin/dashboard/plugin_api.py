"""Authenticated Hermes dashboard bridge for Game Host Console.

Mounted by Hermes at /api/plugins/game-host-console. It serves the console UI
inside Hermes Desktop and proxies only an explicit set of typed local endpoints.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

router = APIRouter()

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
SERVICE_BASE = os.environ.get("GAME_HOST_SERVICE_URL", "http://127.0.0.1:5057").rstrip("/")
PLUGIN_BASE = "/api/plugins/game-host-console"
GET_PATHS = {"health", "api/status", "api/controls"}
POST_PATHS = {"api/control/plan", "api/control/apply"}
MAX_BODY = 65_536


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


def _proxy(method: str, path: str, body: bytes | None = None) -> Response:
    allowed = GET_PATHS if method == "GET" else POST_PATHS
    if path not in allowed:
        raise HTTPException(status_code=404, detail="Proxy route is not allowed")
    request = urllib.request.Request(
        f"{SERVICE_BASE}/{path}",
        data=body,
        method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=320) as upstream:
            content = upstream.read(MAX_BODY + 1)
            if len(content) > MAX_BODY:
                raise HTTPException(status_code=502, detail="Upstream response exceeded safety limit")
            return Response(
                content=content,
                status_code=upstream.status,
                media_type="application/json",
                headers={"Cache-Control": "no-store"},
            )
    except urllib.error.HTTPError as exc:
        content = exc.read(MAX_BODY + 1)
        return Response(
            content=content[:MAX_BODY],
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
    body = await request.body()
    if len(body) > MAX_BODY:
        raise HTTPException(status_code=413, detail="Request body exceeded safety limit")
    return _proxy("POST", path, body)
