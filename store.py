"""Persistent "installed game" set for the Game Host Console.

The console ships a store catalog of every supported game. ``InstalledStore``
tracks which games the user has deliberately added to their console (via the
Store or via Hermes using the game-host skill). Only installed games appear in
the main sidebar; everything else stays in the Store until added.

The installed flag is orthogonal to filesystem readiness: a game can be
installed but not yet provisioned (server files/scripts missing). Persistence is
a small JSON file written atomically. This never holds secrets.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _default_path() -> Path:
    return Path(
        os.environ.get("GAME_HOST_INSTALLED_PATH", "data/installed.json")
    ).expanduser()


class InstalledStore:
    """Read/write the persisted set of installed game ids."""

    def __init__(self, path: Path | None = None, seed: set[str] | None = None) -> None:
        self._path = Path(path) if path is not None else _default_path()
        self._lock = threading.Lock()
        if seed is not None and not self._path.exists():
            self._write(sorted(seed))

    def _read(self) -> set[str]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return set()
        except (OSError, json.JSONDecodeError):
            # A corrupt state file must not take the whole console down; treat
            # it as empty and let the next install rewrite it cleanly.
            return set()
        if not isinstance(raw, dict):
            return set()
        values = raw.get("installed", [])
        if not isinstance(values, list):
            return set()
        return {str(value) for value in values if isinstance(value, str)}

    def installed_ids(self) -> set[str]:
        with self._lock:
            return self._read()

    def is_installed(self, game_id: str) -> bool:
        return game_id in self.installed_ids()

    def install(self, game_id: str) -> None:
        with self._lock:
            current = self._read()
            current.add(game_id)
            self._write(sorted(current))

    def uninstall(self, game_id: str) -> None:
        with self._lock:
            current = self._read()
            current.discard(game_id)
            self._write(sorted(current))

    def _write(self, ids: list[str]) -> None:
        document: dict[str, Any] = {
            "schemaVersion": "1",
            "generatedAt": datetime.now(timezone.utc).astimezone().isoformat(
                timespec="seconds"
            ),
            "installed": ids,
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(self._path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self._path)
