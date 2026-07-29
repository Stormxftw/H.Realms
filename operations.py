"""Durable SQLite operation records for Hermes Game Host Console."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

SCHEMA_VERSION = 1
DEFAULT_OUTPUT_LIMIT = 16_384
STATES = frozenset(
    {"queued", "running", "succeeded", "failed", "cancelled", "outcome_unknown"}
)
TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "outcome_unknown"})
LEGAL_TRANSITIONS = {
    "queued": frozenset({"running", "failed", "cancelled"}),
    "running": TERMINAL_STATES,
    "succeeded": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
    "outcome_unknown": frozenset(),
}
_UNSET = object()
_SECRET_NAMES = r"password|passwd|pwd|token|secret|api[-_]?key|access[-_]?key|private[-_]?key|key"
_SECRET_KEY_RE = re.compile(rf"(?:{_SECRET_NAMES})", re.IGNORECASE)
_QUOTED_SECRET_ASSIGNMENT_RE = re.compile(
    rf'''(?i)(["'](?:{_SECRET_NAMES})["']\s*:\s*)(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;&]+)'''
)
_SECRET_ASSIGNMENT_RE = re.compile(
    rf'''(?i)\b({_SECRET_NAMES})\b(\s*[:=]\s*)(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;&]+)'''
)
_SECRET_OPTION_RE = re.compile(
    rf"(?i)(--?(?:{_SECRET_NAMES})\s+)([^\s]+)"
)
_AUTHORIZATION_RE = re.compile(r"(?i)(authorization\s*:\s*(?:bearer|basic)\s+)[^\s,;&]+")
_TRUNCATION_MARKER = "\n...[truncated]"


class OperationStoreError(RuntimeError):
    """Raised when durable operation storage is unavailable or invalid."""


class InvalidTransitionError(OperationStoreError):
    """Raised when an operation state change is not legal."""


def default_db_path() -> Path:
    state_home = os.environ.get("XDG_STATE_HOME")
    root = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return root / "hermes-game-host-console" / "operations.db"


class OperationStore:
    """Small, thread-safe-enough SQLite store using one connection per call."""

    def __init__(
        self,
        db_path: str | os.PathLike[str] | None = None,
        *,
        schema_path: str | os.PathLike[str] | None = None,
        output_limit: int = DEFAULT_OUTPUT_LIMIT,
        clock: Callable[[], datetime] | None = None,
        private_path_prefixes: Iterable[str | os.PathLike[str]] | None = None,
    ) -> None:
        self.db_path = Path(db_path).expanduser() if db_path is not None else default_db_path()
        self.schema_path = (
            Path(schema_path)
            if schema_path is not None
            else Path(__file__).resolve().parent / "data" / "schema.sql"
        )
        if output_limit < 1:
            raise ValueError("output_limit must be positive")
        self.output_limit = output_limit
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        prefixes = (
            private_path_prefixes
            if private_path_prefixes is not None
            else (Path.home(), Path(__file__).resolve().parent)
        )
        normalized_prefixes = {
            str(Path(prefix).expanduser().absolute()).rstrip(os.sep)
            for prefix in prefixes
            if str(prefix)
        }
        self._private_path_prefixes = tuple(
            sorted((prefix for prefix in normalized_prefixes if prefix != os.sep), key=len, reverse=True)
        )
        self._prepare_storage()
        self._initialize_schema()

    def _prepare_storage(self) -> None:
        try:
            self.db_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            os.chmod(self.db_path.parent, 0o700)
        except (NotImplementedError, OSError) as exc:
            if not self.db_path.parent.is_dir():
                raise OperationStoreError(
                    f"cannot create operation state directory {self.db_path.parent}: {exc}"
                ) from exc

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize_schema(self) -> None:
        try:
            schema_sql = self.schema_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise OperationStoreError(f"cannot read operation schema {self.schema_path}: {exc}") from exc

        connection = self._connect()
        try:
            current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current_version > SCHEMA_VERSION:
                raise OperationStoreError(
                    "operation database schema version "
                    f"{current_version} is newer than supported version {SCHEMA_VERSION}; writes blocked"
                )
            if current_version not in (0, SCHEMA_VERSION):
                raise OperationStoreError(
                    "operation database schema version "
                    f"{current_version} cannot be migrated to {SCHEMA_VERSION}; writes blocked"
                )
            if current_version == 0:
                migration_started = False

                def authorize_migration(
                    action_code: int,
                    argument: str | None,
                    _argument2: str | None,
                    _database_name: str | None,
                    _trigger_name: str | None,
                ) -> int:
                    nonlocal migration_started
                    if action_code == sqlite3.SQLITE_TRANSACTION:
                        if (argument or "").upper() == "BEGIN" and not migration_started:
                            migration_started = True
                            return sqlite3.SQLITE_OK
                        return sqlite3.SQLITE_DENY
                    if action_code == sqlite3.SQLITE_SAVEPOINT:
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK

                try:
                    # executescript commits any pending transaction before it starts. Put
                    # BEGIN in the script and deny transaction control from schema.sql so
                    # an embedded COMMIT cannot make a partial migration durable.
                    connection.set_authorizer(authorize_migration)
                    try:
                        connection.executescript("BEGIN IMMEDIATE;\n" + schema_sql)
                    finally:
                        connection.set_authorizer(None)
                    self._validate_schema(connection)
                    connection.commit()
                except (sqlite3.Error, OperationStoreError) as exc:
                    connection.rollback()
                    raise OperationStoreError(
                        f"operation database migration to schema {SCHEMA_VERSION} failed; writes blocked: {exc}"
                    ) from exc
            else:
                self._validate_schema(connection)
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
        except sqlite3.Error as exc:
            raise OperationStoreError(f"cannot initialize operation database {self.db_path}: {exc}") from exc
        finally:
            connection.close()

        try:
            os.chmod(self.db_path, 0o600)
        except (NotImplementedError, OSError):
            pass

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        expected = {
            "operation_id",
            "game_id",
            "action",
            "actor",
            "source",
            "state",
            "created_at",
            "started_at",
            "finished_at",
            "output",
            "precondition_json",
            "postcondition_json",
            "recovery_note",
        }
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(operations)").fetchall()
        }
        if columns != expected:
            raise OperationStoreError(
                "operation database schema does not match the supported operations table"
            )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _timestamp(self) -> str:
        return self._now().isoformat()

    def _sanitize_text(self, value: str, *, truncate: bool = False) -> str:
        value = _QUOTED_SECRET_ASSIGNMENT_RE.sub(r'\1"[REDACTED]"', value)
        value = _SECRET_ASSIGNMENT_RE.sub(r"\1\2[REDACTED]", value)
        value = _SECRET_OPTION_RE.sub(r"\1[REDACTED]", value)
        value = _AUTHORIZATION_RE.sub(r"\1[REDACTED]", value)
        for prefix in self._private_path_prefixes:
            value = value.replace(prefix, "[PRIVATE_PATH]")
        if truncate and len(value) > self.output_limit:
            if self.output_limit <= len(_TRUNCATION_MARKER):
                return _TRUNCATION_MARKER[-self.output_limit :]
            value = value[: self.output_limit - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER
        return value

    def _sanitize_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._sanitize_text(value)
        if isinstance(value, dict):
            return {
                key: (
                    "[REDACTED]"
                    if isinstance(key, str) and _SECRET_KEY_RE.fullmatch(key)
                    else self._sanitize_value(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self._sanitize_value(item) for item in value]
        return value

    def _encode_json(self, value: Any) -> str | None:
        if value is None:
            return None
        try:
            return json.dumps(
                self._sanitize_value(value),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("operation conditions must be JSON-serializable") from exc

    @staticmethod
    def _decode_json(value: str | None) -> Any:
        return None if value is None else json.loads(value)

    @classmethod
    def _row_to_record(cls, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "operationId": row["operation_id"],
            "gameId": row["game_id"],
            "action": row["action"],
            "actor": row["actor"],
            "source": row["source"],
            "state": row["state"],
            "createdAt": row["created_at"],
            "startedAt": row["started_at"],
            "finishedAt": row["finished_at"],
            "output": row["output"],
            "precondition": cls._decode_json(row["precondition_json"]),
            "postcondition": cls._decode_json(row["postcondition_json"]),
            "recoveryNote": row["recovery_note"],
        }

    def create(
        self,
        *,
        game_id: str,
        action: str,
        actor: str,
        source: str = "unknown",
        operation_id: str | None = None,
        state: str = "queued",
        output: str | None = None,
        precondition: Any = None,
        postcondition: Any = None,
        recovery_note: str | None = None,
    ) -> dict[str, Any]:
        if state not in STATES:
            raise ValueError(f"unknown operation state: {state}")
        for field_name, value in (
            ("game_id", game_id),
            ("action", action),
            ("actor", actor),
            ("source", source),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be non-empty text")
        if output is not None and not isinstance(output, str):
            raise ValueError("operation output must be text or None")
        if recovery_note is not None and not isinstance(recovery_note, str):
            raise ValueError("operation recovery note must be text or None")

        operation_id = operation_id or str(uuid.uuid4())
        created_at = self._timestamp()
        started_at = created_at if state == "running" else None
        finished_at = created_at if state in TERMINAL_STATES else None
        values = (
            operation_id,
            game_id,
            action,
            actor,
            source,
            state,
            created_at,
            started_at,
            finished_at,
            None if output is None else self._sanitize_text(output, truncate=True),
            self._encode_json(precondition),
            self._encode_json(postcondition),
            None if recovery_note is None else self._sanitize_text(recovery_note),
        )
        connection = self._connect()
        try:
            connection.execute(
                """
                INSERT INTO operations (
                    operation_id, game_id, action, actor, source, state,
                    created_at, started_at, finished_at, output,
                    precondition_json, postcondition_json, recovery_note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            connection.commit()
        except sqlite3.Error as exc:
            connection.rollback()
            raise OperationStoreError(f"cannot create operation {operation_id}: {exc}") from exc
        finally:
            connection.close()
        record = self.get(operation_id)
        if record is None:
            raise OperationStoreError(f"created operation {operation_id} could not be read back")
        return record

    def get(self, operation_id: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
        except sqlite3.Error as exc:
            raise OperationStoreError(f"cannot read operation {operation_id}: {exc}") from exc
        finally:
            connection.close()
        return None if row is None else self._row_to_record(row)

    def list(
        self,
        *,
        limit: int = 100,
        game_id: str | None = None,
        state: str | Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be an integer from 1 through 500")

        clauses: list[str] = []
        parameters: list[Any] = []
        if game_id is not None:
            clauses.append("game_id = ?")
            parameters.append(game_id)
        if state is not None:
            states = [state] if isinstance(state, str) else list(state)
            if not states or any(item not in STATES for item in states):
                raise ValueError("state filter contains an unknown operation state")
            placeholders = ", ".join("?" for _ in states)
            clauses.append(f"state IN ({placeholders})")
            parameters.extend(states)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        parameters.append(limit)

        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM operations"
                + where
                + " ORDER BY created_at DESC, operation_id DESC LIMIT ?",
                parameters,
            ).fetchall()
        except sqlite3.Error as exc:
            raise OperationStoreError(f"cannot list operations: {exc}") from exc
        finally:
            connection.close()
        return [self._row_to_record(row) for row in rows]

    def transition(
        self,
        operation_id: str,
        state: str,
        *,
        output: str | None | object = _UNSET,
        postcondition: Any = _UNSET,
        recovery_note: str | None | object = _UNSET,
    ) -> dict[str, Any]:
        if state not in STATES:
            raise InvalidTransitionError(f"unknown operation state: {state}")

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            if row is None:
                raise OperationStoreError(f"operation not found: {operation_id}")
            old_state = row["state"]
            if state not in LEGAL_TRANSITIONS[old_state]:
                raise InvalidTransitionError(
                    f"illegal operation transition {old_state} -> {state} for {operation_id}"
                )

            now = self._timestamp()
            started_at = row["started_at"]
            if state == "running" and started_at is None:
                started_at = now
            finished_at = now if state in TERMINAL_STATES else row["finished_at"]
            if output is _UNSET:
                output_value = row["output"]
            elif output is None:
                output_value = None
            elif isinstance(output, str):
                output_value = self._sanitize_text(output, truncate=True)
            else:
                raise ValueError("operation output must be text or None")
            postcondition_value = (
                row["postcondition_json"]
                if postcondition is _UNSET
                else self._encode_json(postcondition)
            )
            if recovery_note is _UNSET:
                recovery_note_value = row["recovery_note"]
            elif recovery_note is None:
                recovery_note_value = None
            elif isinstance(recovery_note, str):
                recovery_note_value = self._sanitize_text(recovery_note)
            else:
                raise ValueError("operation recovery note must be text or None")
            connection.execute(
                """
                UPDATE operations
                   SET state = ?, started_at = ?, finished_at = ?, output = ?,
                       postcondition_json = ?, recovery_note = ?
                 WHERE operation_id = ?
                """,
                (
                    state,
                    started_at,
                    finished_at,
                    output_value,
                    postcondition_value,
                    recovery_note_value,
                    operation_id,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            connection.commit()
        except (sqlite3.Error, OperationStoreError) as exc:
            connection.rollback()
            if isinstance(exc, OperationStoreError):
                raise
            raise OperationStoreError(f"cannot transition operation {operation_id}: {exc}") from exc
        finally:
            connection.close()
        if updated is None:
            raise OperationStoreError(f"transitioned operation {operation_id} could not be read back")
        return self._row_to_record(updated)

    def recover_interrupted(
        self,
        recovery_note: str = "interrupted by host restart; outcome requires reconciliation",
    ) -> int:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE operations
                   SET state = 'outcome_unknown', finished_at = ?, recovery_note = ?
                 WHERE state = 'running'
                """,
                (self._timestamp(), self._sanitize_text(recovery_note)),
            )
            recovered = cursor.rowcount
            connection.commit()
        except sqlite3.Error as exc:
            connection.rollback()
            raise OperationStoreError(f"cannot recover interrupted operations: {exc}") from exc
        finally:
            connection.close()
        return recovered

    def prune(self, *, retention_days: int = 30, batch_limit: int = 1_000) -> int:
        if isinstance(retention_days, bool) or not isinstance(retention_days, int):
            raise ValueError("retention_days must be a non-negative integer")
        if retention_days < 0:
            raise ValueError("retention_days must be a non-negative integer")
        if isinstance(batch_limit, bool) or not isinstance(batch_limit, int):
            raise ValueError("batch_limit must be a positive integer")
        if not 1 <= batch_limit <= 10_000:
            raise ValueError("batch_limit must be from 1 through 10000")

        cutoff = (self._now() - timedelta(days=retention_days)).isoformat()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                DELETE FROM operations
                 WHERE operation_id IN (
                    SELECT operation_id
                      FROM operations
                     WHERE state IN ('succeeded', 'failed', 'cancelled', 'outcome_unknown')
                       AND finished_at < ?
                     ORDER BY finished_at ASC, operation_id ASC
                     LIMIT ?
                 )
                """,
                (cutoff, batch_limit),
            )
            pruned = cursor.rowcount
            connection.commit()
        except sqlite3.Error as exc:
            connection.rollback()
            raise OperationStoreError(f"cannot prune operation history: {exc}") from exc
        finally:
            connection.close()
        return pruned
