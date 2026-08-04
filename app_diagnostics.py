"""Diagnostics endpoint surface: read-only log tails and bundles per game.

This wires the production-grade `DiagnosticsCollector` into the HTTP layer as
pure helpers. Safety lives in three places and nowhere else:

  1. Path confinement — the collector opens approved logs under the game's
     project root with no-follow directory traversal; the allow-list here only
     ever names project-relative log files.
  2. Bounded reads — byte/line limits come from the collector.
  3. Redaction by default — the console surface redacts IPs unless explicitly
     told otherwise (the bundle is always redacted).

Everything here is read-only, so it does not go through the plan/apply mutation
model. Unknown games / unapproved log ids surface as DiagnosticsNotFoundError,
which the HTTP layer maps to 404.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import diagnostics


class DiagnosticsNotFoundError(LookupError):
    """The game or log identifier is not served by the diagnostics surface."""


def _project_root(engine: Any, game_id: str) -> Path:
    """Resolve the project's on-disk root for an installed game.

    ``engine.game_view`` raises for an unknown game id; translate that to a
    not-found so the HTTP layer returns 404 rather than 400.
    """
    try:
        engine.game_view(game_id)
    except Exception as exc:
        raise DiagnosticsNotFoundError(f"unknown game: {game_id}") from exc
    try:
        project_path = engine._registry.project_paths[game_id]
    except (KeyError, AttributeError) as exc:
        raise DiagnosticsNotFoundError(f"unknown game: {game_id}") from exc
    root = Path(project_path).resolve()
    if not root.is_dir():
        raise DiagnosticsNotFoundError(f"game has no project directory: {game_id}")
    return root


def _approved_logs(project_root: Path) -> dict[str, str]:
    """Build the diagnostics allow-list for one game.

    Only `logs/*.log` regular files directly beneath the project root are
    approved, keyed by their filename. The collector independently re-validates
    every path (no-follow, no traversal, regular file), so this list is the
    human-facing surface, not the security boundary.
    """
    approved: dict[str, str] = {}
    logs_dir = project_root / "logs"
    if logs_dir.is_dir():
        for entry in sorted(logs_dir.iterdir()):
            if entry.is_file() and not entry.is_symlink() and entry.suffix == ".log":
                log_id = entry.stem if entry.stem != "palworld-linux" else "server"
                approved.setdefault(log_id, f"logs/{entry.name}")
    # Always expose the canonical server log under the stable id "server" when
    # present, even if the filename convention changes.
    canonical = project_root / "logs" / "palworld-linux.log"
    if canonical.is_file() and not canonical.is_symlink():
        approved["server"] = "logs/palworld-linux.log"
    return approved


def _collector(project_root: Path) -> diagnostics.DiagnosticsCollector:
    return diagnostics.DiagnosticsCollector(
        project_root,
        _approved_logs(project_root),
    )


def list_diagnostics_logs(engine: Any, game_id: str) -> dict[str, Any]:
    """Return the approved log ids and their project-relative paths."""
    root = _project_root(engine, game_id)
    return {
        "gameId": game_id,
        "logs": _approved_logs(root),
    }


def diagnostics_log_tail(
    engine: Any,
    game_id: str,
    log_id: str,
    *,
    redact_ips: bool = True,
) -> dict[str, Any]:
    """Return a bounded, redacted tail for one approved log."""
    root = _project_root(engine, game_id)
    collector = _collector(root)
    try:
        return collector.tail(log_id, redact_ips=redact_ips)
    except diagnostics.UnknownLogError as exc:
        raise DiagnosticsNotFoundError(
            f"log id is not approved for {game_id}: {log_id}"
        ) from exc


def diagnostics_bundle(
    engine: Any,
    game_id: str,
    *,
    version: Any = "unknown",
) -> str:
    """Return a redacted diagnostics bundle for one game."""
    root = _project_root(engine, game_id)
    collector = _collector(root)
    game = engine.game_view(game_id)
    tails = []
    for log_id in collector.approved_log_ids:
        try:
            tails.append(collector.tail(log_id, redact_ips=True))
        except diagnostics.UnknownLogError:
            continue
    return collector.build_bundle(
        version=version,
        readiness_blockers=game.get("blockers", ()),
        capabilities=game.get("capabilities"),
        log_tails=tails,
        redact_ips=True,
    )
