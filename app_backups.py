"""Backup endpoint surface: inventory, verified create, and restore preview.

This wires the production-grade `BackupManager` into the HTTP layer as pure
helpers. The manager enforces its own confinement invariant: source and backup
directories are approved, non-overlapping, *relative* paths beneath a single
project root, archives are validated and published atomically, and creation is
proven by an inventory transition.

Two game layouts are supported, driven by the adapter:

  - Self-contained game (default): the project root is the game's own project
    dir; the source is `Pal/Saved` when present, else the whole project dir;
    backups land in `<project>/backups/`.
  - External server tree (e.g. Palworld): the adapter declares a `backup`
    block with an absolute `projectDir` (the real server root), a relative
    `sourceDir`, and a relative `backupDir`. The manager then confines within
    that external tree — the confinement invariant is preserved, not bypassed.

Restore *preview* is exposed (read-only, returns the confirmation token).
Restore *execute* is NOT exposed — it overwrites live data and stays unwired
until it is deliberately warranted.

Unknown games / artifacts surface as BackupNotFoundError (404). Business-rule
failures (bad label, confinement, verification) surface as BackupOperationError
(400).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import backups


class BackupNotFoundError(LookupError):
    """The game or artifact is not served by the backup surface."""


class BackupOperationError(RuntimeError):
    """A backup operation violated policy or failed verification."""


def _adapter(engine: Any, game_id: str) -> dict[str, Any]:
    try:
        return engine._adapter_for(game_id)
    except Exception as exc:
        raise BackupNotFoundError(f"unknown game: {game_id}") from exc


def _project_root(engine: Any, game_id: str) -> Path:
    """Resolve the on-disk project root the manager confines within.

    Default is the validated registry project path. An adapter `backup` block
    may point at an external server root (absolute path) for games whose live
    data does not live under the console's project dir.
    """
    try:
        engine.game_view(game_id)
    except Exception as exc:
        raise BackupNotFoundError(f"unknown game: {game_id}") from exc

    adapter = _adapter(engine, game_id)
    backup_block = adapter.get("backup")
    if isinstance(backup_block, dict) and backup_block.get("projectDir"):
        candidate = Path(str(backup_block["projectDir"])).expanduser()
    else:
        try:
            project_path = engine._registry.project_paths[game_id]
        except (KeyError, AttributeError) as exc:
            raise BackupNotFoundError(f"unknown game: {game_id}") from exc
        candidate = Path(project_path)

    root = candidate.resolve()
    if not root.is_dir():
        raise BackupNotFoundError(f"game has no project directory: {game_id}")
    return root


def _relative(adapter_value: Any, default: str) -> Path:
    raw = str(adapter_value).strip() if adapter_value is not None else ""
    return Path(raw) if raw else Path(default)


def _policy(engine: Any, project_root: Path, game_id: str) -> backups.BackupPolicy:
    adapter = _adapter(engine, game_id)
    backup_block = adapter.get("backup") if isinstance(adapter.get("backup"), dict) else {}

    if backup_block:
        source = _relative(backup_block.get("sourceDir"), "Pal/Saved")
        backup_dir = _relative(backup_block.get("backupDir"), "backups")
    else:
        # Self-contained game: back up the mutable saved tree when present,
        # else the project state dir. Both are non-empty relative paths.
        saved = Path("Pal") / "Saved"
        source = saved if (project_root / saved).is_dir() else Path("state")
        backup_dir = Path("backups")

    return backups.BackupPolicy(
        project_root=project_root,
        source_relative=source,
        backup_relative=backup_dir,
        archive_type=backups.ArchiveType.TAR_GZ,
        source_identity=game_id,
    )


def _manager(engine: Any, project_root: Path, game_id: str) -> backups.BackupManager:
    """Build the manager, creating the backup directory on first use.

    The manager validates that its approved directories already exist; the
    backup destination is created lazily here (inside the confinement root) so
    a game with no backups yet still returns an empty inventory.
    """
    policy = _policy(engine, project_root, game_id)
    (project_root / policy.backup_relative).mkdir(parents=True, exist_ok=True)
    try:
        return backups.BackupManager(policy)
    except backups.BackupError as exc:
        raise BackupOperationError(str(exc)) from exc


def _artifact_dict(artifact: backups.BackupArtifact) -> dict[str, Any]:
    return {
        "artifactId": artifact.artifact_id,
        "filename": artifact.filename,
        "createdAt": artifact.created_at,
        "sizeBytes": artifact.size_bytes,
        "validation": {
            "state": artifact.validation.state,
            "reason": artifact.validation.reason,
            "entryCount": artifact.validation.entry_count,
        },
        "sourceIdentity": artifact.source_identity,
    }


def list_backups(engine: Any, game_id: str) -> dict[str, Any]:
    """Return the verified artifact inventory for one game."""
    root = _project_root(engine, game_id)
    manager = _manager(engine, root, game_id)
    try:
        artifacts = manager.inventory()
    except backups.BackupError as exc:
        raise BackupOperationError(str(exc)) from exc
    return {
        "gameId": game_id,
        "backups": [_artifact_dict(artifact) for artifact in artifacts],
    }


def create_backup(
    engine: Any, game_id: str, *, label: str = "manual"
) -> dict[str, Any]:
    """Create one verified archive of the game's approved source."""
    root = _project_root(engine, game_id)
    manager = _manager(engine, root, game_id)
    try:
        artifact = manager.create_backup(label=label)
    except backups.BackupError as exc:
        raise BackupOperationError(str(exc)) from exc
    return _artifact_dict(artifact)


def preview_restore(
    engine: Any,
    game_id: str,
    artifact_id: str,
    *,
    server_state: str,
) -> dict[str, Any]:
    """Authorize a stopped-server restore without mutating the source.

    Returns the preview id and the exact confirmation token the eventual
    execute step would require. Read-only.
    """
    root = _project_root(engine, game_id)
    manager = _manager(engine, root, game_id)
    try:
        state = backups.ServerState(server_state)
    except ValueError as exc:
        raise BackupOperationError(f"invalid server state: {server_state}") from exc
    try:
        preview = manager.preview_restore(artifact_id, server_state=state)
    except backups.ArtifactNotFoundError as exc:
        raise BackupNotFoundError(f"unknown backup artifact: {artifact_id}") from exc
    except backups.BackupError as exc:
        raise BackupOperationError(str(exc)) from exc
    return {
        "previewId": preview.preview_id,
        "artifactId": preview.artifact_id,
        "sourceIdentity": preview.source_identity,
        "archiveEntries": list(preview.archive_entries),
        "willReplace": list(preview.will_replace),
        "requiredConfirmation": preview.required_confirmation,
    }
