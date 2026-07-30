"""Path-confined backup inventory and guarded local restore primitives.

The module deliberately accepts paths only through :class:`BackupPolicy`.  Public
artifact operations use opaque artifact IDs rather than caller-supplied paths.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import tarfile
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal, Sequence


class BackupError(RuntimeError):
    """Base error for backup and restore operations."""


class BackupPolicyError(BackupError):
    """The configured backup policy is unsafe or invalid."""


class BackupVerificationError(BackupError):
    """A claimed backup creation could not be proven from inventory evidence."""


class ConfinementError(BackupError):
    """An approved path no longer resolves inside its confinement root."""


class RestoreError(BackupError):
    """A guarded restore could not complete safely."""


class ServerStateError(RestoreError):
    """Restore was attempted without a stopped server state."""


class ConfirmationError(RestoreError):
    """Restore confirmation did not exactly match its preview."""


class ArtifactNotFoundError(RestoreError):
    """An opaque artifact ID is not present in the approved inventory."""


class ArchiveType(str, Enum):
    ZIP = "zip"
    TAR_GZ = "tar.gz"

    @property
    def suffix(self) -> str:
        return ".zip" if self is ArchiveType.ZIP else ".tar.gz"


class ServerState(str, Enum):
    STOPPED = "stopped"
    RUNNING = "running"
    DEGRADED = "degraded"


@dataclass(frozen=True)
class BackupPolicy:
    project_root: Path
    source_relative: Path
    backup_relative: Path
    archive_type: ArchiveType
    source_identity: str

    def __post_init__(self) -> None:
        project_root = Path(os.path.abspath(os.fspath(self.project_root)))
        source = Path(self.source_relative)
        backup = Path(self.backup_relative)
        for description, relative in (("source", source), ("backup", backup)):
            if (
                relative.is_absolute()
                or relative == Path(".")
                or any(part in ("", ".", "..") for part in relative.parts)
            ):
                raise BackupPolicyError(
                    f"{description} path must be a non-empty relative path beneath project root"
                )
        if source == backup or source.is_relative_to(backup) or backup.is_relative_to(source):
            raise BackupPolicyError("source and backup paths must not overlap")
        if not isinstance(self.archive_type, ArchiveType):
            try:
                archive_type = ArchiveType(self.archive_type)
            except (TypeError, ValueError) as exc:
                raise BackupPolicyError("unsupported archive type") from exc
            object.__setattr__(self, "archive_type", archive_type)
        if not self.source_identity or not self.source_identity.strip():
            raise BackupPolicyError("source identity must be non-empty")
        object.__setattr__(self, "project_root", project_root)
        object.__setattr__(self, "source_relative", source)
        object.__setattr__(self, "backup_relative", backup)
        object.__setattr__(self, "source_identity", self.source_identity.strip())


@dataclass(frozen=True)
class ValidationResult:
    state: Literal["valid", "invalid"]
    reason: str
    entry_count: int = 0

    @property
    def valid(self) -> bool:
        return self.state == "valid"


@dataclass(frozen=True)
class BackupArtifact:
    artifact_id: str
    filename: str
    created_at: str
    size_bytes: int
    validation: ValidationResult
    source_identity: str


@dataclass(frozen=True)
class RestorePreview:
    preview_id: str
    artifact_id: str
    source_identity: str
    archive_entries: tuple[str, ...]
    will_replace: tuple[str, ...]
    required_confirmation: str


@dataclass(frozen=True)
class RestoreResult:
    artifact_id: str
    source_identity: str
    safety_backup: BackupArtifact
    restored_entries: tuple[str, ...]


def verify_created(
    before_inventory: Sequence[BackupArtifact],
    after_inventory: Sequence[BackupArtifact],
    *,
    source_identity: str,
) -> BackupArtifact:
    """Prove that an inventory transition added exactly one usable artifact."""

    before_ids = {artifact.artifact_id for artifact in before_inventory}
    after_by_id = {artifact.artifact_id: artifact for artifact in after_inventory}
    if len(after_by_id) != len(after_inventory):
        raise BackupVerificationError("after inventory contains duplicate artifact IDs")
    if not before_ids.issubset(after_by_id):
        raise BackupVerificationError("prior backup artifacts changed or disappeared")
    new_ids = set(after_by_id) - before_ids
    if len(new_ids) != 1:
        raise BackupVerificationError("backup creation must add exactly one new artifact")
    created = after_by_id[new_ids.pop()]
    if created.size_bytes <= 0:
        raise BackupVerificationError("created backup artifact is empty")
    if not created.validation.valid:
        raise BackupVerificationError(
            f"created backup artifact is invalid: {created.validation.reason}"
        )
    if created.source_identity != source_identity:
        raise BackupVerificationError("created backup source identity does not match policy")
    return created


class BackupManager:
    """Inventory and mutation surface bound to one approved backup policy."""

    def __init__(
        self,
        policy: BackupPolicy,
        *,
        max_entries: int = 10_000,
        max_metadata_bytes: int = 8 * 1024 * 1024,
        max_uncompressed_bytes: int = 1024 * 1024 * 1024,
    ) -> None:
        if min(max_entries, max_metadata_bytes, max_uncompressed_bytes) <= 0:
            raise BackupPolicyError("archive validation limits must be positive")
        self.policy = policy
        self.max_entries = max_entries
        self.max_metadata_bytes = max_metadata_bytes
        self.max_uncompressed_bytes = max_uncompressed_bytes
        root = self._validate_directory(policy.project_root, description="project root")
        self._root_identity = self._identity(root)
        source = self._resolve_approved_directory(
            policy.source_relative, description="source directory"
        )
        backup = self._resolve_approved_directory(
            policy.backup_relative, description="backup directory"
        )
        self._source_initial_identity = self._identity(source)
        self._backup_identity = self._identity(backup)
        self._restore_previews: dict[str, RestorePreview] = {}

    def inventory(self) -> tuple[BackupArtifact, ...]:
        backup_dir = self._backup_directory()
        artifacts: list[BackupArtifact] = []
        for path in sorted(backup_dir.iterdir(), key=lambda item: item.name):
            if not path.name.endswith(self.policy.archive_type.suffix):
                continue
            artifacts.append(self._artifact_for_path(path))
        artifacts.sort(key=lambda item: (item.created_at, item.filename), reverse=True)
        return tuple(artifacts)

    def create_backup(self, *, label: str = "manual") -> BackupArtifact:
        """Create one archive of the approved source and verify inventory evidence."""

        safe_label = re.sub(r"[^a-z0-9-]+", "-", label.strip().lower()).strip("-")
        if not safe_label:
            raise BackupPolicyError("backup label must contain a letter or digit")
        before = self.inventory()
        source = self._source_directory()
        self._validate_readable_tree(source)
        backup_dir = self._backup_directory()
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        filename = (
            f"{safe_label}-{timestamp}-{uuid.uuid4().hex[:8]}"
            f"{self.policy.archive_type.suffix}"
        )
        final_path = backup_dir / filename
        temporary_path = backup_dir / f".{filename}.{uuid.uuid4().hex}.tmp"
        try:
            if self.policy.archive_type is ArchiveType.ZIP:
                self._write_zip_backup(source, temporary_path)
            else:
                self._write_tar_backup(source, temporary_path)
            # Revalidate both endpoints immediately before publishing the archive.
            self._source_directory()
            current_backup_dir = self._backup_directory()
            if final_path.parent != current_backup_dir:
                raise ConfinementError("backup destination escaped approved directory")
            os.replace(temporary_path, final_path)
        except BaseException:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        after = self.inventory()
        return verify_created(
            before, after, source_identity=self.policy.source_identity
        )

    def preview_restore(
        self, artifact_id: str, *, server_state: ServerState
    ) -> RestorePreview:
        """Authorize one exact, stopped-server restore without mutating the source."""

        self._require_stopped(server_state)
        self._source_directory()
        artifact, artifact_path = self._resolve_artifact(artifact_id)
        archive_entries = self._archive_entry_names(artifact_path)
        preview_id = f"restore-{uuid.uuid4().hex}"
        confirmation_token = uuid.uuid4().hex
        preview = RestorePreview(
            preview_id=preview_id,
            artifact_id=artifact.artifact_id,
            source_identity=self.policy.source_identity,
            archive_entries=archive_entries,
            will_replace=tuple(
                f"{self.policy.source_relative.as_posix()}/{entry}"
                for entry in archive_entries
            ),
            required_confirmation=(
                f"RESTORE {self.policy.source_identity} FROM {artifact.artifact_id} "
                f"TOKEN {confirmation_token}"
            ),
        )
        self._restore_previews[preview_id] = preview
        return preview

    def execute_restore(
        self,
        preview_id: str,
        *,
        confirmation: str,
        server_state: ServerState,
    ) -> RestoreResult:
        """Execute a previewed restore with a verified safety backup and rollback."""

        self._require_stopped(server_state)
        preview = self._restore_previews.get(preview_id)
        if preview is None or confirmation != preview.required_confirmation:
            raise ConfirmationError("restore confirmation does not match its preview")

        self._source_directory()
        artifact, artifact_path = self._resolve_artifact(preview.artifact_id)
        archive_entries = self._archive_entry_names(artifact_path)
        if (
            artifact.source_identity != preview.source_identity
            or archive_entries != preview.archive_entries
        ):
            raise RestoreError("restore preview no longer matches the approved artifact")

        try:
            before_safety_backup = self.inventory()
            claimed_safety_backup = self.create_backup(label="pre-restore")
            after_safety_backup = self.inventory()
            safety_backup = verify_created(
                before_safety_backup,
                after_safety_backup,
                source_identity=self.policy.source_identity,
            )
            if claimed_safety_backup.artifact_id != safety_backup.artifact_id:
                raise BackupVerificationError(
                    "created safety backup does not match inventory evidence"
                )
        except BackupError as exc:
            raise RestoreError(
                "restore stopped because a verified safety backup could not be created"
            ) from exc

        # A safety backup can take time. Revalidate every approved endpoint and the
        # exact content-addressed artifact again before preparing any mutation.
        source = self._source_directory()
        artifact, artifact_path = self._resolve_artifact(preview.artifact_id)
        archive_entries = self._archive_entry_names(artifact_path)
        if archive_entries != preview.archive_entries:
            raise RestoreError("restore artifact changed after safety backup creation")

        project_root = self._project_root()
        staging = Path(tempfile.mkdtemp(prefix=".ghc-restore-", dir=project_root))
        staged_source = staging / "source"
        rollback = project_root / (
            f".{self.policy.source_relative.name}.pre-restore-{uuid.uuid4().hex}"
        )
        original_moved = False
        replacement_moved = False
        try:
            staged_source.mkdir()
            restored_entries = self._extract_archive(artifact_path, staged_source)
            if restored_entries != tuple(sorted(preview.archive_entries)):
                raise RestoreError("extracted entries do not match the restore preview")
            self._validate_readable_tree(staged_source)

            # Revalidate immediately before the first source-tree mutation.
            source = self._source_directory()
            self._backup_directory()
            current_artifact, _ = self._resolve_artifact(preview.artifact_id)
            if current_artifact.artifact_id != artifact.artifact_id:
                raise RestoreError("restore artifact changed before source replacement")
            if os.path.lexists(rollback):
                raise RestoreError("temporary rollback path already exists")

            os.replace(source, rollback)
            original_moved = True
            os.replace(staged_source, source)
            replacement_moved = True
            try:
                self._verify_restored_source(source)
            except BaseException as exc:
                self._restore_original_source(
                    source=source,
                    rollback=rollback,
                    staging=staging,
                    replacement_moved=replacement_moved,
                )
                original_moved = False
                replacement_moved = False
                raise RestoreError(
                    "restore verification failed; restored original source"
                ) from exc

            self._remove_tree(rollback)
            original_moved = False
            self._source_initial_identity = self._identity(source)
            self._restore_previews.pop(preview_id, None)
            return RestoreResult(
                artifact_id=artifact.artifact_id,
                source_identity=self.policy.source_identity,
                safety_backup=safety_backup,
                restored_entries=restored_entries,
            )
        except BaseException as exc:
            if original_moved:
                try:
                    self._restore_original_source(
                        source=source,
                        rollback=rollback,
                        staging=staging,
                        replacement_moved=replacement_moved,
                    )
                    original_moved = False
                except BaseException as rollback_exc:
                    raise RestoreError(
                        "restore failed and the original source could not be rolled back"
                    ) from rollback_exc
                raise RestoreError("restore failed; restored original source") from exc
            raise
        finally:
            self._remove_tree(staging, missing_ok=True)

    @staticmethod
    def _require_stopped(server_state: ServerState) -> None:
        if server_state is not ServerState.STOPPED:
            raise ServerStateError("server must be stopped before restore")

    def _resolve_artifact(self, artifact_id: str) -> tuple[BackupArtifact, Path]:
        matches = tuple(
            artifact for artifact in self.inventory() if artifact.artifact_id == artifact_id
        )
        if len(matches) != 1:
            raise ArtifactNotFoundError("artifact ID is not present in approved inventory")
        artifact = matches[0]
        if not artifact.validation.valid:
            raise RestoreError(
                f"approved restore artifact is invalid: {artifact.validation.reason}"
            )

        backup_dir = self._backup_directory()
        artifact_path = backup_dir / artifact.filename
        if artifact_path.parent != backup_dir:
            raise ConfinementError("restore artifact escaped approved backup directory")
        current = self._artifact_for_path(artifact_path)
        if current.artifact_id != artifact_id:
            raise ArtifactNotFoundError("approved restore artifact changed")
        if not current.validation.valid:
            raise RestoreError(
                f"approved restore artifact is invalid: {current.validation.reason}"
            )
        return current, artifact_path

    def _archive_entry_names(self, path: Path) -> tuple[str, ...]:
        validation = self._validate_archive(path)
        if not validation.valid:
            raise RestoreError(f"restore archive is invalid: {validation.reason}")
        names: list[str] = []
        try:
            if self.policy.archive_type is ArchiveType.ZIP:
                with self._open_regular_nofollow(path) as raw, zipfile.ZipFile(
                    raw, "r"
                ) as archive:
                    for entry in archive.infolist():
                        parts = self._member_parts(entry.filename)
                        mode = entry.external_attr >> 16
                        file_type = stat.S_IFMT(mode)
                        if stat.S_ISLNK(mode):
                            raise ValueError("archive contains a symlink entry")
                        if file_type and file_type not in (stat.S_IFREG, stat.S_IFDIR):
                            raise ValueError("archive contains a special-device entry")
                        names.append(PurePosixPath(*parts).as_posix())
            else:
                with self._open_regular_nofollow(path) as raw, tarfile.open(
                    fileobj=raw, mode="r:gz"
                ) as archive:
                    for entry in archive:
                        parts = self._member_parts(entry.name)
                        if entry.issym() or entry.islnk():
                            raise ValueError("archive contains a link entry")
                        if not (entry.isfile() or entry.isdir()):
                            raise ValueError("archive contains a special-device entry")
                        names.append(PurePosixPath(*parts).as_posix())
        except (OSError, ValueError, zipfile.BadZipFile, tarfile.TarError) as exc:
            raise RestoreError(f"restore archive became invalid: {exc}") from exc
        return tuple(sorted(names))

    def _extract_archive(self, path: Path, destination: Path) -> tuple[str, ...]:
        expected_entries = self._archive_entry_names(path)
        try:
            if self.policy.archive_type is ArchiveType.ZIP:
                restored_entries = self._extract_zip(path, destination)
            else:
                restored_entries = self._extract_tar_gz(path, destination)
        except (OSError, ValueError, zipfile.BadZipFile, tarfile.TarError) as exc:
            raise RestoreError(f"could not safely extract restore archive: {exc}") from exc
        restored = tuple(sorted(restored_entries))
        if restored != expected_entries:
            raise RestoreError("restore archive changed during extraction")
        return restored

    def _extract_zip(self, path: Path, destination: Path) -> list[str]:
        restored: list[str] = []
        directory_modes: list[tuple[Path, int]] = []
        with self._open_regular_nofollow(path) as raw, zipfile.ZipFile(raw, "r") as archive:
            for entry in archive.infolist():
                parts = self._member_parts(entry.filename)
                restored.append(PurePosixPath(*parts).as_posix())
                target = destination.joinpath(*parts)
                mode = entry.external_attr >> 16
                file_type = stat.S_IFMT(mode)
                if stat.S_ISLNK(mode):
                    raise ValueError("archive contains a symlink entry")
                if file_type and file_type not in (stat.S_IFREG, stat.S_IFDIR):
                    raise ValueError("archive contains a special-device entry")
                permissions = stat.S_IMODE(mode) & 0o777
                is_directory = entry.is_dir() or stat.S_ISDIR(mode)
                if is_directory:
                    target.mkdir(parents=True, exist_ok=True)
                    if not target.is_dir() or target.is_symlink():
                        raise ValueError("archive directory conflicts with another entry")
                    directory_modes.append((target, permissions))
                    continue

                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry, "r") as source_file, target.open("xb") as target_file:
                    shutil.copyfileobj(source_file, target_file, 1024 * 1024)
                if permissions:
                    target.chmod(permissions)
        for directory, permissions in sorted(
            directory_modes, key=lambda item: len(item[0].parts), reverse=True
        ):
            if permissions:
                directory.chmod(permissions)
        return restored

    def _extract_tar_gz(self, path: Path, destination: Path) -> list[str]:
        restored: list[str] = []
        directory_modes: list[tuple[Path, int]] = []
        with self._open_regular_nofollow(path) as raw, tarfile.open(
            fileobj=raw, mode="r:gz"
        ) as archive:
            for entry in archive:
                parts = self._member_parts(entry.name)
                restored.append(PurePosixPath(*parts).as_posix())
                target = destination.joinpath(*parts)
                if entry.issym() or entry.islnk():
                    raise ValueError("archive contains a link entry")
                if not (entry.isfile() or entry.isdir()):
                    raise ValueError("archive contains a special-device entry")
                permissions = stat.S_IMODE(entry.mode) & 0o777
                if entry.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    if not target.is_dir() or target.is_symlink():
                        raise ValueError("archive directory conflicts with another entry")
                    directory_modes.append((target, permissions))
                    continue

                extracted = archive.extractfile(entry)
                if extracted is None:
                    raise ValueError(f"archive member is unreadable: {entry.name}")
                target.parent.mkdir(parents=True, exist_ok=True)
                with extracted, target.open("xb") as target_file:
                    shutil.copyfileobj(extracted, target_file, 1024 * 1024)
                if permissions:
                    target.chmod(permissions)
        for directory, permissions in sorted(
            directory_modes, key=lambda item: len(item[0].parts), reverse=True
        ):
            if permissions:
                directory.chmod(permissions)
        return restored

    def _verify_restored_source(self, source: Path) -> None:
        restored = self._validate_directory(source, description="restored source")
        project_root = self._project_root()
        if restored == project_root or not restored.is_relative_to(project_root):
            raise ConfinementError("restored source escaped approved project root")
        self._validate_readable_tree(restored)

    def _restore_original_source(
        self,
        *,
        source: Path,
        rollback: Path,
        staging: Path,
        replacement_moved: bool,
    ) -> None:
        failed_source = staging / "failed-source"
        if replacement_moved or os.path.lexists(source):
            if os.path.lexists(failed_source):
                self._remove_tree(failed_source)
            os.replace(source, failed_source)
        if not os.path.lexists(rollback):
            raise RestoreError("original source rollback tree is unavailable")
        os.replace(rollback, source)
        self._source_initial_identity = self._identity(source)

    @staticmethod
    def _remove_tree(path: Path, *, missing_ok: bool = False) -> None:
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            if missing_ok:
                return
            raise
        if stat.S_ISDIR(mode) and not stat.S_ISLNK(mode):
            shutil.rmtree(path)
        else:
            path.unlink()

    def _source_directory(self) -> Path:
        source = self._resolve_approved_directory(
            self.policy.source_relative, description="source directory"
        )
        if self._identity(source) != self._source_initial_identity:
            raise ConfinementError("approved source directory identity changed")
        return source

    def _validate_readable_tree(self, root: Path) -> None:
        self._reject_symlink_components(root, description="source directory")
        for path in sorted(root.rglob("*")):
            self._reject_symlink_components(path, description="source tree")
            try:
                mode = path.lstat().st_mode
            except OSError as exc:
                raise BackupError("source tree became unavailable") from exc
            if stat.S_ISDIR(mode):
                continue
            if not stat.S_ISREG(mode):
                raise ConfinementError("source tree contains a non-regular entry")
            try:
                with path.open("rb") as source_file:
                    source_file.read(1)
            except OSError as exc:
                raise BackupError("source tree contains an unreadable file") from exc

    def _write_zip_backup(self, source: Path, destination: Path) -> None:
        with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(source.rglob("*")):
                mode = path.lstat().st_mode
                if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                    raise ConfinementError("source tree changed during backup creation")
                relative = path.relative_to(source).as_posix()
                archive.write(path, arcname=relative)

    def _write_tar_backup(self, source: Path, destination: Path) -> None:
        with tarfile.open(destination, "x:gz") as archive:
            for path in sorted(source.rglob("*")):
                mode = path.lstat().st_mode
                if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                    raise ConfinementError("source tree changed during backup creation")
                archive.add(
                    path,
                    arcname=path.relative_to(source).as_posix(),
                    recursive=False,
                )

    @staticmethod
    def _identity(path: Path) -> tuple[int, int]:
        details = path.stat(follow_symlinks=False)
        return details.st_dev, details.st_ino

    @staticmethod
    def _reject_symlink_components(path: Path, *, description: str) -> None:
        absolute = Path(os.path.abspath(os.fspath(path)))
        for component in reversed((absolute, *absolute.parents)):
            try:
                mode = component.lstat().st_mode
            except OSError as exc:
                raise ConfinementError(f"approved {description} is unavailable") from exc
            if stat.S_ISLNK(mode):
                raise ConfinementError(
                    f"approved {description} contains a symlink component: {component}"
                )

    def _validate_directory(self, path: Path, *, description: str) -> Path:
        self._reject_symlink_components(path, description=description)
        try:
            resolved = path.resolve(strict=True)
            mode = path.lstat().st_mode
        except (OSError, RuntimeError) as exc:
            raise ConfinementError(f"approved {description} is unavailable") from exc
        if not stat.S_ISDIR(mode):
            raise ConfinementError(f"approved {description} is not a directory")
        return resolved

    def _project_root(self) -> Path:
        root = self._validate_directory(self.policy.project_root, description="project root")
        if self._identity(root) != self._root_identity:
            raise ConfinementError("approved project root identity changed")
        return root

    def _resolve_approved_directory(self, relative: Path, *, description: str) -> Path:
        root = self._project_root() if hasattr(self, "_root_identity") else self._validate_directory(
            self.policy.project_root, description="project root"
        )
        configured = self.policy.project_root / relative
        resolved = self._validate_directory(configured, description=description)
        if resolved == root or not resolved.is_relative_to(root):
            raise ConfinementError(
                f"approved {description} no longer resolves beneath project root"
            )
        return resolved

    def _backup_directory(self) -> Path:
        backup = self._resolve_approved_directory(
            self.policy.backup_relative, description="backup directory"
        )
        if self._identity(backup) != self._backup_identity:
            raise ConfinementError("approved backup directory identity changed")
        return backup

    def _artifact_for_path(self, path: Path) -> BackupArtifact:
        try:
            details = path.lstat()
        except OSError as exc:
            raise BackupError(f"could not inspect backup artifact {path.name}") from exc
        validation = self._validate_archive(path)
        artifact_id = self._artifact_id(path, details, validation.valid)
        timestamp = datetime.fromtimestamp(details.st_mtime, tz=timezone.utc)
        return BackupArtifact(
            artifact_id=artifact_id,
            filename=path.name,
            created_at=timestamp.isoformat().replace("+00:00", "Z"),
            size_bytes=details.st_size,
            validation=validation,
            source_identity=self.policy.source_identity,
        )

    def _artifact_id(self, path: Path, details: os.stat_result, readable: bool) -> str:
        digest = hashlib.sha256()
        digest.update(self.policy.source_identity.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.name.encode("utf-8", "surrogateescape"))
        digest.update(b"\0")
        if readable and stat.S_ISREG(details.st_mode):
            try:
                with self._open_regular_nofollow(path) as archive:
                    for chunk in iter(lambda: archive.read(1024 * 1024), b""):
                        digest.update(chunk)
            except OSError:
                readable = False
        if not readable:
            digest.update(
                f"{details.st_mode}:{details.st_size}:{details.st_mtime_ns}".encode("ascii")
            )
        return f"bak-{digest.hexdigest()[:32]}"

    @staticmethod
    def _open_regular_nofollow(path: Path):
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode):
                raise OSError("artifact is not a regular file")
            return os.fdopen(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _member_parts(name: str) -> tuple[str, ...]:
        if not name or "\x00" in name:
            raise ValueError("archive contains an empty or NUL member name")
        normalized = name.replace("\\", "/")
        posix = PurePosixPath(normalized)
        if (
            normalized.startswith("/")
            or PureWindowsPath(name).drive
            or any(part == ".." for part in posix.parts)
        ):
            raise ValueError(f"archive member is absolute or traverses: {name!r}")
        parts = tuple(part for part in posix.parts if part not in ("", ".", "/"))
        if not parts:
            raise ValueError("archive contains an empty member name")
        return parts

    def _validate_archive(self, path: Path) -> ValidationResult:
        try:
            details = path.lstat()
            if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
                raise ValueError("artifact must be a regular non-symlink file")
            if details.st_size <= 0:
                raise ValueError("artifact is empty")
            if self.policy.archive_type is ArchiveType.ZIP:
                return self._validate_zip(path)
            return self._validate_tar_gz(path)
        except tarfile.TarError as exc:
            return ValidationResult("invalid", f"malformed archive: {exc}")
        except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as exc:
            return ValidationResult("invalid", str(exc) or exc.__class__.__name__)

    def _validate_zip(self, path: Path) -> ValidationResult:
        with self._open_regular_nofollow(path) as raw, zipfile.ZipFile(raw, "r") as archive:
            entries = archive.infolist()
            if not entries:
                raise ValueError("archive contains no entries")
            if len(entries) > self.max_entries:
                raise ValueError("archive entry count exceeds limit")
            metadata_bytes = len(archive.comment)
            uncompressed_bytes = 0
            file_entries = 0
            seen: set[tuple[str, ...]] = set()
            for entry in entries:
                parts = self._member_parts(entry.filename)
                if parts in seen:
                    raise ValueError("archive contains duplicate member paths")
                seen.add(parts)
                metadata_bytes += len(entry.filename.encode("utf-8", "surrogateescape"))
                metadata_bytes += len(entry.extra) + len(entry.comment)
                if metadata_bytes > self.max_metadata_bytes:
                    raise ValueError("archive metadata exceeds limit")
                mode = entry.external_attr >> 16
                file_type = stat.S_IFMT(mode)
                if stat.S_ISLNK(mode):
                    raise ValueError("archive contains a symlink entry")
                if file_type and file_type not in (stat.S_IFREG, stat.S_IFDIR):
                    raise ValueError("archive contains a special-device entry")
                if entry.flag_bits & 0x1:
                    raise ValueError("archive contains an encrypted entry")
                if not entry.is_dir():
                    file_entries += 1
                    uncompressed_bytes += entry.file_size
                    if uncompressed_bytes > self.max_uncompressed_bytes:
                        raise ValueError("archive uncompressed data exceeds limit")
            if not file_entries:
                raise ValueError("archive contains no file entries")
            corrupt = archive.testzip()
            if corrupt is not None:
                raise ValueError(f"archive member is unreadable: {corrupt}")
            return ValidationResult("valid", "ok", len(entries))

    def _validate_tar_gz(self, path: Path) -> ValidationResult:
        entry_count = 0
        metadata_bytes = 0
        uncompressed_bytes = 0
        file_entries = 0
        seen: set[tuple[str, ...]] = set()
        with self._open_regular_nofollow(path) as raw, tarfile.open(
            fileobj=raw, mode="r:gz"
        ) as archive:
            for entry in archive:
                entry_count += 1
                if entry_count > self.max_entries:
                    raise ValueError("archive entry count exceeds limit")
                parts = self._member_parts(entry.name)
                if parts in seen:
                    raise ValueError("archive contains duplicate member paths")
                seen.add(parts)
                metadata_bytes += len(entry.name.encode("utf-8", "surrogateescape"))
                metadata_bytes += len(entry.linkname.encode("utf-8", "surrogateescape"))
                metadata_bytes += sum(
                    len(str(key).encode("utf-8")) + len(str(value).encode("utf-8"))
                    for key, value in entry.pax_headers.items()
                )
                if metadata_bytes > self.max_metadata_bytes:
                    raise ValueError("archive metadata exceeds limit")
                if entry.issym() or entry.islnk():
                    raise ValueError("archive contains a link entry")
                if not (entry.isfile() or entry.isdir()):
                    raise ValueError("archive contains a special-device entry")
                if entry.isfile():
                    file_entries += 1
                    if entry.size < 0:
                        raise ValueError("archive contains an invalid file size")
                    uncompressed_bytes += entry.size
                    if uncompressed_bytes > self.max_uncompressed_bytes:
                        raise ValueError("archive uncompressed data exceeds limit")
                    extracted = archive.extractfile(entry)
                    if extracted is None:
                        raise ValueError(f"archive member is unreadable: {entry.name}")
                    read_bytes = 0
                    with extracted:
                        while True:
                            chunk = extracted.read(1024 * 1024)
                            if not chunk:
                                break
                            read_bytes += len(chunk)
                    if read_bytes != entry.size:
                        raise ValueError(f"archive member is unreadable: {entry.name}")
        if entry_count == 0:
            raise ValueError("archive contains no entries")
        if file_entries == 0:
            raise ValueError("archive contains no file entries")
        return ValidationResult("valid", "ok", entry_count)


__all__: Sequence[str] = (
    "ArchiveType",
    "ArtifactNotFoundError",
    "BackupArtifact",
    "BackupError",
    "BackupManager",
    "BackupPolicy",
    "BackupPolicyError",
    "BackupVerificationError",
    "ConfirmationError",
    "ConfinementError",
    "RestoreError",
    "RestorePreview",
    "RestoreResult",
    "ServerState",
    "ServerStateError",
    "ValidationResult",
    "verify_created",
)
