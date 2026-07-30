"""Bounded, path-confined diagnostics for approved game-server logs."""

from __future__ import annotations

import errno
import os
import re
import stat
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any


DEFAULT_MAX_BYTES = 64 * 1024
DEFAULT_MAX_LINES = 200
DEFAULT_BUNDLE_MAX_BYTES = 32 * 1024

_SECRET_NAMES = (
    r"password|passwd|pwd|token|secret|api[-_]?key|x[-_]?api[-_]?key|"
    r"access[-_]?key|private[-_]?key|client[-_]?secret|key"
)
_SECRET_ASSIGNMENT_RE = re.compile(
    rf'''(?i)(\b(?:{_SECRET_NAMES})\b\s*[:=]\s*)'''
    rf'''(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;&]+)'''
)
_QUOTED_SECRET_ASSIGNMENT_RE = re.compile(
    rf'''(?i)(["'](?:{_SECRET_NAMES})["']\s*:\s*)'''
    rf'''(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;&]+)'''
)
_SECRET_OPTION_RE = re.compile(
    rf"(?i)(--?(?:{_SECRET_NAMES})\s+)([^\s,;&]+)"
)
_AUTHORIZATION_RE = re.compile(
    r"(?i)(\bauthorization\s*:\s*(?:bearer|basic)\s+)[^\s,;&]+"
)
_BEARER_RE = re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]+")
_TOKEN_SHAPE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|"
    r"AKIA[A-Z0-9]{16})(?![A-Za-z0-9])"
)
_ANSI_ESCAPE_RE = re.compile(
    r"\x1B(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))"
)
_IPV4_RE = re.compile(
    r"(?<![\w.])(?:25[0-5]|2[0-4]\d|1?\d?\d)"
    r"(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}(?![\w.])"
)
_PRIVATE_POSIX_PATH_RE = re.compile(
    r"(?<![\w])/(?:home|Users|run/media)/[^\s,;:'\"]+"
)
_PRIVATE_WINDOWS_PATH_RE = re.compile(
    r"(?i)(?<![\w])(?:[A-Z]:\\Users\\|\\\\[^\\\s]+\\)[^\s,;:'\"]+"
)


class DiagnosticsError(ValueError):
    """Base error for invalid diagnostics configuration or requests."""


class DiagnosticsPathError(DiagnosticsError):
    """Raised when an approved log path is not safely project-relative."""


class UnknownLogError(DiagnosticsError):
    """Raised when a caller requests a log that was not approved up front."""


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def _timestamp_iso(epoch: float) -> str:
    return _utc_iso(datetime.fromtimestamp(epoch, timezone.utc))


def _validate_positive(name: str, value: int) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _normalize_controls(value: str) -> str:
    value = _ANSI_ESCAPE_RE.sub("", value)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    normalized: list[str] = []
    for character in value:
        if character == "\n":
            normalized.append(character)
        elif character == "\t":
            normalized.append(" ")
        elif not unicodedata.category(character).startswith("C"):
            normalized.append(character)
    return "".join(normalized)


def redact_text(
    value: str,
    *,
    redact_ips: bool = False,
    redact_private_paths: bool = False,
    private_path_prefixes: Iterable[str | os.PathLike[str]] = (),
) -> str:
    """Normalize controls and redact common credentials from diagnostic text."""
    if not isinstance(value, str):
        raise TypeError("diagnostic text must be a string")
    value = _normalize_controls(value)
    value = _AUTHORIZATION_RE.sub(r"\1[REDACTED]", value)
    value = _BEARER_RE.sub(r"\1[REDACTED]", value)
    value = _QUOTED_SECRET_ASSIGNMENT_RE.sub(r"\1\"[REDACTED]\"", value)
    value = _SECRET_ASSIGNMENT_RE.sub(r"\1[REDACTED]", value)
    value = _SECRET_OPTION_RE.sub(r"\1[REDACTED]", value)
    value = _TOKEN_SHAPE_RE.sub("[REDACTED]", value)
    if redact_ips:
        value = _IPV4_RE.sub("[REDACTED_IP]", value)
    if redact_private_paths:
        prefixes = sorted(
            {str(prefix) for prefix in private_path_prefixes if str(prefix)},
            key=len,
            reverse=True,
        )
        for prefix in prefixes:
            value = value.replace(prefix, "[PRIVATE_PATH]")
        value = _PRIVATE_POSIX_PATH_RE.sub("[PRIVATE_PATH]", value)
        value = _PRIVATE_WINDOWS_PATH_RE.sub("[PRIVATE_PATH]", value)
    return value


def _decode_text_tail(payload: bytes, *, started_after_zero: bool) -> str | None:
    if b"\x00" in payload:
        return None
    disallowed_controls = sum(
        byte < 32 and byte not in (9, 10, 13, 27) for byte in payload
    )
    if payload and disallowed_controls * 4 > len(payload):
        return None
    offsets = range(0, min(4, len(payload) + 1)) if started_after_zero else (0,)
    for offset in offsets:
        try:
            return payload[offset:].decode("utf-8")
        except UnicodeDecodeError:
            continue
    return None


def _bounded_utf8_tail(value: str, byte_limit: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= byte_limit:
        return value, False
    encoded = encoded[-byte_limit:]
    for offset in range(min(4, len(encoded))):
        try:
            return encoded[offset:].decode("utf-8"), True
        except UnicodeDecodeError:
            continue
    return "", True


def _bounded_utf8_head(value: str, byte_limit: int, marker: str) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= byte_limit:
        return value
    marker_bytes = marker.encode("utf-8")
    if len(marker_bytes) >= byte_limit:
        return marker_bytes[:byte_limit].decode("utf-8", errors="ignore")
    prefix = encoded[: byte_limit - len(marker_bytes)]
    while prefix:
        try:
            return prefix.decode("utf-8") + marker
        except UnicodeDecodeError:
            prefix = prefix[:-1]
    return marker


_FORBIDDEN_BUNDLE_KEYS = frozenset(
    {
        "apikey",
        "command",
        "commands",
        "config",
        "configuration",
        "credential",
        "credentials",
        "output",
        "password",
        "privatekey",
        "save",
        "savedata",
        "saves",
        "secret",
        "stderr",
        "stdin",
        "stdout",
        "token",
    }
)
_SUMMARY_FIELDS = (
    "code",
    "probe",
    "capability",
    "state",
    "message",
    "reason",
)
_OPERATION_FIELDS = (
    "operationId",
    "gameId",
    "action",
    "state",
    "createdAt",
    "startedAt",
    "finishedAt",
    "recoveryNote",
)
_BUNDLE_TRUNCATION_MARKER = "\n...[diagnostics truncated]\n"


def _key_is_forbidden(key: str) -> bool:
    canonical = re.sub(r"[^a-z0-9]", "", key.lower())
    return canonical in _FORBIDDEN_BUNDLE_KEYS


def _bundle_scalar(
    value: Any,
    *,
    redact_ips: bool,
    private_path_prefixes: Iterable[str | os.PathLike[str]],
    byte_limit: int = 1024,
) -> str | None:
    if value is None:
        text = "none"
    elif type(value) is bool:
        text = "true" if value else "false"
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        text = str(value)
    elif isinstance(value, str):
        text = value
    else:
        return None
    text = redact_text(
        text,
        redact_ips=redact_ips,
        redact_private_paths=True,
        private_path_prefixes=private_path_prefixes,
    )
    return _bounded_utf8_head(text, byte_limit, "...[truncated]")


def _bundle_summary(
    item: Any,
    fields: tuple[str, ...],
    *,
    redact_ips: bool,
    private_path_prefixes: Iterable[str | os.PathLike[str]],
) -> str:
    if isinstance(item, Mapping):
        parts: list[str] = []
        for field in fields:
            if field not in item or _key_is_forbidden(field):
                continue
            value = _bundle_scalar(
                item[field],
                redact_ips=redact_ips,
                private_path_prefixes=private_path_prefixes,
            )
            if value is not None:
                parts.append(f"{field}={value}")
        return ", ".join(parts) or "summary unavailable"
    value = _bundle_scalar(
        item,
        redact_ips=redact_ips,
        private_path_prefixes=private_path_prefixes,
    )
    return value or "summary unavailable"


def _safe_unconfined_log_identifier(value: Any) -> bool:
    if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
        return False
    path = Path(value)
    windows_path = PureWindowsPath(value)
    return not (
        path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or ".." in path.parts
        or "\\" in value
    )


def build_diagnostics_bundle(
    *,
    version: Any,
    readiness_blockers: Iterable[Any] = (),
    capabilities: Mapping[str, Any] | None = None,
    telemetry_probe_errors: Iterable[Any] = (),
    active_operation: Mapping[str, Any] | None = None,
    recent_operations: Iterable[Mapping[str, Any]] = (),
    log_tails: Iterable[Mapping[str, Any]] = (),
    max_bytes: int = DEFAULT_BUNDLE_MAX_BYTES,
    redact_ips: bool = False,
    approved_log_ids: Iterable[str] | None = None,
    private_path_prefixes: Iterable[str | os.PathLike[str]] = (),
) -> str:
    """Build a deterministic, copyable bundle from summary-only diagnostics data."""
    max_bytes = _validate_positive("max_bytes", max_bytes)
    private_paths = tuple(private_path_prefixes)
    approved = set(approved_log_ids) if approved_log_ids is not None else None
    safe_version = _bundle_scalar(
        version,
        redact_ips=redact_ips,
        private_path_prefixes=private_paths,
        byte_limit=256,
    ) or "unknown"
    lines = ["Hermes Game Host Console diagnostics", f"Version: {safe_version}"]

    lines.extend(("", "[Readiness blockers]"))
    blocker_items = list(readiness_blockers)[:50]
    if blocker_items:
        lines.extend(
            "- "
            + _bundle_summary(
                item,
                _SUMMARY_FIELDS,
                redact_ips=redact_ips,
                private_path_prefixes=private_paths,
            )
            for item in blocker_items
        )
    else:
        lines.append("- none")

    lines.extend(("", "[Capabilities]"))
    safe_capabilities = []
    for key, raw_value in sorted((capabilities or {}).items(), key=lambda item: str(item[0])):
        if not isinstance(key, str) or _key_is_forbidden(key):
            continue
        value = _bundle_scalar(
            raw_value,
            redact_ips=redact_ips,
            private_path_prefixes=private_paths,
        )
        if value is not None:
            safe_capabilities.append(f"{key}: {value}")
        if len(safe_capabilities) >= 100:
            break
    lines.extend(safe_capabilities or ("none",))

    lines.extend(("", "[Telemetry probe errors]"))
    telemetry_items = list(telemetry_probe_errors)[:50]
    if telemetry_items:
        lines.extend(
            "- "
            + _bundle_summary(
                item,
                _SUMMARY_FIELDS,
                redact_ips=redact_ips,
                private_path_prefixes=private_paths,
            )
            for item in telemetry_items
        )
    else:
        lines.append("- none")

    lines.extend(("", "[Active operation]"))
    if active_operation:
        lines.append(
            "- "
            + _bundle_summary(
                active_operation,
                _OPERATION_FIELDS,
                redact_ips=redact_ips,
                private_path_prefixes=private_paths,
            )
        )
    else:
        lines.append("- none")

    lines.extend(("", "[Recent operations]"))
    recent_items = list(recent_operations)[:25]
    if recent_items:
        lines.extend(
            "- "
            + _bundle_summary(
                item,
                _OPERATION_FIELDS,
                redact_ips=redact_ips,
                private_path_prefixes=private_paths,
            )
            for item in recent_items
        )
    else:
        lines.append("- none")

    lines.extend(("", "[Approved log tails]"))
    safe_tails: list[Mapping[str, Any]] = []
    for tail in log_tails:
        if not isinstance(tail, Mapping):
            continue
        log_id = tail.get("logId")
        if approved is not None:
            if log_id not in approved:
                continue
        elif not _safe_unconfined_log_identifier(log_id):
            continue
        safe_tails.append(tail)
        if len(safe_tails) >= 20:
            break
    safe_tails.sort(key=lambda item: str(item.get("logId")))
    if not safe_tails:
        lines.append("- none")
    for tail in safe_tails:
        log_id = _bundle_scalar(
            tail.get("logId"),
            redact_ips=redact_ips,
            private_path_prefixes=private_paths,
            byte_limit=256,
        ) or "unknown"
        metadata_parts = []
        for field in ("state", "sizeBytes", "modifiedAt", "truncated"):
            value = _bundle_scalar(
                tail.get(field),
                redact_ips=redact_ips,
                private_path_prefixes=private_paths,
                byte_limit=256,
            )
            if value is not None:
                metadata_parts.append(f"{field}={value}")
        lines.append(f"- {log_id}: {', '.join(metadata_parts)}")
        content = _bundle_scalar(
            tail.get("content", ""),
            redact_ips=redact_ips,
            private_path_prefixes=private_paths,
            byte_limit=8192,
        )
        if content:
            lines.extend(f"  {line}" for line in content.splitlines())

    bundle = "\n".join(lines) + "\n"
    return _bounded_utf8_head(bundle, max_bytes, _BUNDLE_TRUNCATION_MARKER)


class DiagnosticsCollector:
    """Read bounded tails from a fixed allow-list beneath one game project root."""

    def __init__(
        self,
        project_root: str | os.PathLike[str],
        approved_logs: Mapping[str, str | os.PathLike[str]]
        | Iterable[str | os.PathLike[str]],
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_lines: int = DEFAULT_MAX_LINES,
        bundle_max_bytes: int = DEFAULT_BUNDLE_MAX_BYTES,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        supplied_root = Path(project_root).expanduser()
        if supplied_root.is_symlink():
            raise DiagnosticsPathError("game project root must not be a symlink")
        try:
            self._root = supplied_root.resolve(strict=True)
        except OSError as exc:
            raise DiagnosticsPathError("game project root is unavailable") from exc
        if not self._root.is_dir():
            raise DiagnosticsPathError("game project root must be a directory")

        self.max_bytes = _validate_positive("max_bytes", max_bytes)
        self.max_lines = _validate_positive("max_lines", max_lines)
        self.bundle_max_bytes = _validate_positive(
            "bundle_max_bytes", bundle_max_bytes
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))

        if isinstance(approved_logs, Mapping):
            entries = approved_logs.items()
        else:
            entries = ((Path(item).as_posix(), item) for item in approved_logs)
        self._approved: dict[str, tuple[str, ...]] = {}
        for log_id, relative_path in entries:
            if not isinstance(log_id, str) or not log_id or any(
                ord(character) < 32 for character in log_id
            ):
                raise DiagnosticsPathError("approved log identifier is invalid")
            if log_id in self._approved:
                raise DiagnosticsPathError("approved log identifiers must be unique")
            parts = self._validate_relative_path(relative_path)
            self._approved[log_id] = parts

    @property
    def approved_log_ids(self) -> tuple[str, ...]:
        return tuple(self._approved)

    def _validate_relative_path(
        self, relative_path: str | os.PathLike[str]
    ) -> tuple[str, ...]:
        raw = os.fspath(relative_path)
        if not isinstance(raw, str) or not raw or "\x00" in raw:
            raise DiagnosticsPathError("approved log path is invalid")
        windows_path = PureWindowsPath(raw)
        path = Path(raw)
        if path.is_absolute() or windows_path.is_absolute() or windows_path.drive:
            raise DiagnosticsPathError("approved log path must be relative")
        if "\\" in raw:
            raise DiagnosticsPathError("approved log path must use project-relative separators")
        if any(part in ("", ".", "..") for part in path.parts):
            raise DiagnosticsPathError("approved log path contains traversal")
        if not path.parts:
            raise DiagnosticsPathError("approved log path is invalid")

        current = self._root
        for part in path.parts:
            current = current / part
            try:
                if current.is_symlink():
                    raise DiagnosticsPathError(
                        "approved log path contains a symlink component"
                    )
            except OSError as exc:
                raise DiagnosticsPathError(
                    "approved log path could not be validated"
                ) from exc
        return tuple(path.parts)

    def _base_result(self, log_id: str, state: str) -> dict[str, Any]:
        return {
            "logId": log_id,
            "state": state,
            "collectedAt": _utc_iso(self._clock()),
            "sizeBytes": None,
            "modifiedAt": None,
            "content": "",
            "truncated": False,
            "limits": {
                "maxBytes": self.max_bytes,
                "maxLines": self.max_lines,
            },
        }

    def _open_approved(self, parts: tuple[str, ...]) -> tuple[int, int, str]:
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        directory_fd = os.open(self._root, directory_flags | no_follow)
        try:
            for part in parts[:-1]:
                next_fd = os.open(
                    part,
                    directory_flags | no_follow,
                    dir_fd=directory_fd,
                )
                os.close(directory_fd)
                directory_fd = next_fd
            file_fd = os.open(
                parts[-1],
                os.O_RDONLY | no_follow | getattr(os, "O_NONBLOCK", 0),
                dir_fd=directory_fd,
            )
            return file_fd, directory_fd, parts[-1]
        except BaseException:
            os.close(directory_fd)
            raise

    def tail(
        self,
        log_id: str,
        *,
        max_bytes: int | None = None,
        max_lines: int | None = None,
        redact_ips: bool = False,
    ) -> dict[str, Any]:
        """Return a bounded snapshot for one approved identifier."""
        if log_id not in self._approved:
            raise UnknownLogError("log identifier is not approved")
        byte_limit = min(
            self.max_bytes,
            _validate_positive("max_bytes", max_bytes)
            if max_bytes is not None
            else self.max_bytes,
        )
        line_limit = min(
            self.max_lines,
            _validate_positive("max_lines", max_lines)
            if max_lines is not None
            else self.max_lines,
        )
        result = self._base_result(log_id, "ok")
        result["limits"] = {"maxBytes": byte_limit, "maxLines": line_limit}

        try:
            file_fd, directory_fd, filename = self._open_approved(
                self._approved[log_id]
            )
        except FileNotFoundError:
            result["state"] = "missing"
            return result
        except PermissionError:
            result["state"] = "permission_denied"
            return result
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                result["state"] = "unsafe"
                return result
            result["state"] = "unavailable"
            return result

        try:
            metadata = os.fstat(file_fd)
            result["sizeBytes"] = metadata.st_size
            result["modifiedAt"] = _timestamp_iso(metadata.st_mtime)
            if not stat.S_ISREG(metadata.st_mode):
                result["state"] = "not_regular"
                return result
            if metadata.st_size == 0:
                result["state"] = "empty"
                return result

            start = max(0, metadata.st_size - byte_limit)
            payload = os.pread(file_fd, byte_limit, start)
            try:
                current_fd_metadata = os.fstat(file_fd)
                current_path_metadata = os.stat(
                    filename, dir_fd=directory_fd, follow_symlinks=False
                )
            except OSError:
                result["state"] = "rotated"
                return result
            if (
                (metadata.st_dev, metadata.st_ino)
                != (current_fd_metadata.st_dev, current_fd_metadata.st_ino)
                or (metadata.st_dev, metadata.st_ino)
                != (current_path_metadata.st_dev, current_path_metadata.st_ino)
            ):
                result["state"] = "rotated"
                return result

            text = _decode_text_tail(payload, started_after_zero=start > 0)
            if text is None:
                result["state"] = "binary"
                return result
            text = redact_text(text, redact_ips=redact_ips)
            lines = text.splitlines()
            line_truncated = len(lines) > line_limit
            if line_truncated:
                lines = lines[-line_limit:]
            content, output_truncated = _bounded_utf8_tail(
                "\n".join(lines), byte_limit
            )
            result["content"] = content
            result["truncated"] = start > 0 or line_truncated or output_truncated
            if result["truncated"]:
                result["state"] = "too_large"
            return result
        finally:
            os.close(file_fd)
            os.close(directory_fd)

    def build_bundle(
        self,
        *,
        version: Any,
        readiness_blockers: Iterable[Any] = (),
        capabilities: Mapping[str, Any] | None = None,
        telemetry_probe_errors: Iterable[Any] = (),
        active_operation: Mapping[str, Any] | None = None,
        recent_operations: Iterable[Mapping[str, Any]] = (),
        log_tails: Iterable[Mapping[str, Any]] = (),
        max_bytes: int | None = None,
        redact_ips: bool = False,
    ) -> str:
        """Build a bundle that accepts tails only from this collector's allow-list."""
        byte_limit = (
            self.bundle_max_bytes
            if max_bytes is None
            else min(
                self.bundle_max_bytes,
                _validate_positive("max_bytes", max_bytes),
            )
        )
        return build_diagnostics_bundle(
            version=version,
            readiness_blockers=readiness_blockers,
            capabilities=capabilities,
            telemetry_probe_errors=telemetry_probe_errors,
            active_operation=active_operation,
            recent_operations=recent_operations,
            log_tails=log_tails,
            max_bytes=byte_limit,
            redact_ips=redact_ips,
            approved_log_ids=self.approved_log_ids,
            private_path_prefixes=(self._root,),
        )


# Short alias for callers that prefer a domain-level name.
Diagnostics = DiagnosticsCollector
