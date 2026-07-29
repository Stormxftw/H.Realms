from __future__ import annotations

import copy
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn


_STATE_DIRECTORY = "hermes-game-host-console"
_STATE_FILENAME = "restart-state.json"
UNKNOWN_EFFECTIVE_VALUE = object()


def _reject_non_json_constant(value: str) -> None:
    raise ValueError(f"non-JSON constant {value}")


class RestartStateError(RuntimeError):
    """Raised when pending-restart state cannot be read or persisted safely."""


class RestartStateCorruptionError(RestartStateError):
    """Raised when persisted restart state is malformed or structurally invalid."""


def pending_restart_presentation(status: str, pending_count: int) -> dict[str, Any]:
    """Describe pending-restart guidance without invoking lifecycle operations."""
    if pending_count <= 0:
        return {
            "pendingCount": 0,
            "bannerState": "none",
            "showBanner": False,
            "canOfferRestart": False,
            "bannerText": "",
            "nextActionText": "",
        }

    noun = "change" if pending_count == 1 else "changes"
    verb = "requires" if pending_count == 1 else "require"
    banner_text = f"{pending_count} pending {noun} {verb} a verified transition."
    presentations = {
        "running": {
            "bannerState": "restart-required",
            "showBanner": True,
            "canOfferRestart": True,
            "bannerText": banner_text,
            "nextActionText": (
                f"Restart the running server to apply {pending_count} pending {noun}."
            ),
        },
        "stopped": {
            "bannerState": "pending-next-start",
            "showBanner": True,
            "canOfferRestart": False,
            "bannerText": banner_text,
            "nextActionText": (
                f"The next verified start will apply {pending_count} pending {noun}."
            ),
        },
        "degraded": {
            "bannerState": "pending-degraded",
            "showBanner": True,
            "canOfferRestart": False,
            "bannerText": banner_text,
            "nextActionText": (
                "Verify server health, then complete a verified start or restart "
                f"to apply {pending_count} pending {noun}."
            ),
        },
    }
    unknown_presentation = {
        "bannerState": "pending-unknown",
        "showBanner": True,
        "canOfferRestart": False,
        "bannerText": banner_text,
        "nextActionText": (
            "Verify server status, then complete a verified start or restart "
            f"to apply {pending_count} pending {noun}."
        ),
    }
    return {
        "pendingCount": pending_count,
        **presentations.get(status, unknown_presentation),
    }


def default_restart_state_path() -> Path:
    """Return the per-user state path without touching the repository."""
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / _STATE_DIRECTORY / _STATE_FILENAME


def _parse_timestamp(value: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise RestartStateError(f"{field}: expected an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RestartStateError(f"{field}: timestamp must include a UTC offset")
    return parsed


class RestartStateStore:
    """Persistent pending-restart state, isolated from lifecycle execution."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path).expanduser() if path is not None else default_restart_state_path()

    @staticmethod
    def _empty_state() -> dict[str, Any]:
        return {"schemaVersion": 1, "games": {}}

    def _validate_state(self, state: Any) -> dict[str, Any]:
        def invalid(location: str, expectation: str) -> NoReturn:
            raise RestartStateCorruptionError(
                f"{self.path}: restart state is structurally invalid; "
                f"{location} {expectation}"
            )

        if not isinstance(state, dict):
            invalid("$", "must be an object")
        if type(state.get("schemaVersion")) is not int or state["schemaVersion"] != 1:
            invalid("$.schemaVersion", "must equal 1")
        games = state.get("games")
        if not isinstance(games, dict):
            invalid("$.games", "must be an object")

        required_control_fields = {
            "configuredValue",
            "effectiveValue",
            "effectiveValueKnown",
            "originatingOperationId",
            "firstOperationId",
            "latestOperationId",
            "firstChangedAt",
            "changedAt",
            "restartRequired",
            "history",
        }
        for game_id, game in games.items():
            game_location = f"$.games[{game_id!r}]"
            if not isinstance(game_id, str):
                invalid("$.games", "keys must be strings")
            if not isinstance(game, dict):
                invalid(game_location, "must be an object")
            controls = game.get("controls")
            if not isinstance(controls, dict):
                invalid(f"{game_location}.controls", "must be an object")

            for control_id, control in controls.items():
                control_location = f"{game_location}.controls[{control_id!r}]"
                if not isinstance(control_id, str):
                    invalid(f"{game_location}.controls", "keys must be strings")
                if not isinstance(control, dict):
                    invalid(control_location, "must be an object")
                missing = required_control_fields.difference(control)
                if missing:
                    invalid(
                        control_location,
                        f"is missing required fields: {', '.join(sorted(missing))}",
                    )
                if type(control["effectiveValueKnown"]) is not bool:
                    invalid(f"{control_location}.effectiveValueKnown", "must be boolean")
                if type(control["restartRequired"]) is not bool:
                    invalid(f"{control_location}.restartRequired", "must be boolean")
                for field in (
                    "originatingOperationId",
                    "firstOperationId",
                    "latestOperationId",
                    "firstChangedAt",
                    "changedAt",
                ):
                    if not isinstance(control[field], str):
                        invalid(f"{control_location}.{field}", "must be a string")
                history = control["history"]
                if not isinstance(history, list):
                    invalid(f"{control_location}.history", "must be an array")
                for index, entry in enumerate(history):
                    entry_location = f"{control_location}.history[{index}]"
                    if not isinstance(entry, dict):
                        invalid(entry_location, "must be an object")
                    missing_history = {
                        "configuredValue",
                        "operationId",
                        "changedAt",
                    }.difference(entry)
                    if missing_history:
                        invalid(
                            entry_location,
                            "is missing required fields: "
                            f"{', '.join(sorted(missing_history))}",
                        )
                    for field in ("operationId", "changedAt"):
                        if not isinstance(entry[field], str):
                            invalid(f"{entry_location}.{field}", "must be a string")

        return state

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty_state()
        try:
            state = json.loads(
                self.path.read_text(encoding="utf-8"),
                parse_constant=_reject_non_json_constant,
            )
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise RestartStateCorruptionError(
                f"{self.path}: restart state contains invalid JSON"
            ) from exc
        except OSError as exc:
            raise RestartStateError(
                f"{self.path}: could not read restart state: {exc}"
            ) from exc
        return self._validate_state(state)

    def _write(self, state: dict[str, Any]) -> None:
        temporary_path: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if os.name == "posix":
                self.path.parent.chmod(0o700)
            payload = json.dumps(
                state, allow_nan=False, indent=2, sort_keys=True
            ) + "\n"
            descriptor, temporary_name = tempfile.mkstemp(
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                text=True,
            )
            temporary_path = Path(temporary_name)
            if os.name == "posix":
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
            temporary_path = None
            if os.name == "posix":
                self.path.chmod(0o600)
        except (OSError, TypeError, ValueError) as exc:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise RestartStateError(
                f"{self.path}: could not atomically persist restart state: {exc}"
            ) from exc

    def record_change(
        self,
        *,
        game_id: str,
        control_id: str,
        configured_value: Any,
        originating_operation_id: str,
        changed_at: str,
        effective_value: Any = UNKNOWN_EFFECTIVE_VALUE,
    ) -> dict[str, Any]:
        state = self._read()
        controls = state["games"].setdefault(game_id, {"controls": {}})["controls"]
        existing = controls.get(control_id)
        history = copy.deepcopy(existing["history"]) if existing else []
        history.append(
            {
                "configuredValue": copy.deepcopy(configured_value),
                "operationId": originating_operation_id,
                "changedAt": changed_at,
            }
        )
        if effective_value is UNKNOWN_EFFECTIVE_VALUE:
            stored_effective_value = (
                copy.deepcopy(existing["effectiveValue"]) if existing else None
            )
            effective_value_known = (
                bool(existing["effectiveValueKnown"]) if existing else False
            )
        else:
            stored_effective_value = copy.deepcopy(effective_value)
            effective_value_known = True
        controls[control_id] = {
            "configuredValue": copy.deepcopy(configured_value),
            "effectiveValue": stored_effective_value,
            "effectiveValueKnown": effective_value_known,
            "originatingOperationId": originating_operation_id,
            "firstOperationId": (
                existing["firstOperationId"] if existing else originating_operation_id
            ),
            "latestOperationId": originating_operation_id,
            "firstChangedAt": existing["firstChangedAt"] if existing else changed_at,
            "changedAt": changed_at,
            "restartRequired": True,
            "history": history,
        }
        self._write(state)
        return next(
            item
            for item in self.list_pending(game_id)
            if item["controlId"] == control_id
        )

    def list_pending(self, game_id: str) -> list[dict[str, Any]]:
        controls = self._read()["games"].get(game_id, {}).get("controls", {})
        pending = []
        for control_id, stored in controls.items():
            if not stored["restartRequired"]:
                continue
            item = copy.deepcopy(stored)
            item.update(
                {
                    "gameId": game_id,
                    "controlId": control_id,
                    "effectiveValueStatus": (
                        "known" if item["effectiveValueKnown"] else "unknown"
                    ),
                }
            )
            pending.append(item)
        return sorted(pending, key=lambda item: (item["changedAt"], item["controlId"]))

    def mark_verified_transition(
        self,
        game_id: str,
        transition_finished_at: str,
        *,
        successful: bool = True,
    ) -> list[str]:
        """Clear changes adopted by a verified successful start or restart."""
        transition_time = _parse_timestamp(
            transition_finished_at, field="transition_finished_at"
        )
        if not successful:
            return []
        state = self._read()
        game = state["games"].get(game_id)
        if game is None:
            return []
        controls = game["controls"]
        cleared = sorted(
            control_id
            for control_id, item in controls.items()
            if _parse_timestamp(
                item["changedAt"], field=f"{game_id}.{control_id}.changedAt"
            )
            <= transition_time
        )
        for control_id in cleared:
            del controls[control_id]
        if not controls:
            del state["games"][game_id]
        if cleared:
            self._write(state)
        return cleared
