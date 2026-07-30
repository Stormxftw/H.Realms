import io
import os
import stat
import tarfile
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest import mock

import backups


class BackupFoundationTests(unittest.TestCase):
    def setUp(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary_directory.name)
        self.project = self.root / "project"
        self.source = self.project / "world"
        self.backup_dir = self.project / "backups"
        self.source.mkdir(parents=True)
        self.backup_dir.mkdir()
        (self.source / "level.dat").write_bytes(b"original-world")

    def tearDown(self):
        self._temporary_directory.cleanup()

    def manager(self, archive_type=backups.ArchiveType.ZIP, **limits):
        policy = backups.BackupPolicy(
            project_root=self.project,
            source_relative=Path("world"),
            backup_relative=Path("backups"),
            archive_type=archive_type,
            source_identity="minecraft:world",
        )
        return backups.BackupManager(policy, **limits)

    @staticmethod
    def write_zip(path, entries):
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in entries:
                if isinstance(content, zipfile.ZipInfo):
                    archive.writestr(content, b"target")
                else:
                    archive.writestr(name, content)

    @staticmethod
    def write_tar(path, entries):
        with tarfile.open(path, "w:gz") as archive:
            for member, content in entries:
                if isinstance(member, str):
                    info = tarfile.TarInfo(member)
                    info.size = len(content)
                else:
                    info = member
                archive.addfile(info, io.BytesIO(content) if info.isreg() else None)

    def test_inventory_returns_stable_non_path_artifact_with_validation_evidence(self):
        archive_path = self.backup_dir / "world-20260729.zip"
        self.write_zip(archive_path, [("level.dat", b"restorable")])
        manager = self.manager()

        first = manager.inventory()
        second = manager.inventory()

        self.assertEqual(1, len(first))
        artifact = first[0]
        self.assertEqual(artifact.artifact_id, second[0].artifact_id)
        self.assertTrue(artifact.artifact_id.startswith("bak-"))
        self.assertNotIn(str(self.project), artifact.artifact_id)
        self.assertEqual("world-20260729.zip", artifact.filename)
        self.assertGreater(artifact.size_bytes, 0)
        self.assertRegex(artifact.created_at, r"^\d{4}-\d{2}-\d{2}T")
        self.assertEqual("valid", artifact.validation.state)
        self.assertEqual("ok", artifact.validation.reason)
        self.assertEqual(1, artifact.validation.entry_count)
        self.assertEqual("minecraft:world", artifact.source_identity)
        self.assertFalse(hasattr(artifact, "path"))

    def test_policy_rejects_unconfined_relative_paths(self):
        for source, backup_dir in (
            (Path("../outside"), Path("backups")),
            (Path("world"), Path("../outside")),
            (self.root / "absolute-world", Path("backups")),
            (Path("world"), self.root / "absolute-backups"),
        ):
            with self.subTest(source=source, backup_dir=backup_dir), self.assertRaises(
                backups.BackupPolicyError
            ):
                backups.BackupPolicy(
                    project_root=self.project,
                    source_relative=source,
                    backup_relative=backup_dir,
                    archive_type=backups.ArchiveType.ZIP,
                    source_identity="minecraft:world",
                )

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_inventory_rejects_backup_directory_symlink_swap(self):
        manager = self.manager()
        original_backup_dir = self.project / "original-backups"
        self.backup_dir.rename(original_backup_dir)
        outside = self.root / "outside-backups"
        outside.mkdir()
        sentinel = outside / "sentinel"
        sentinel.write_bytes(b"outside")
        self.backup_dir.symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(backups.ConfinementError, "symlink"):
            manager.inventory()

        self.assertEqual(b"outside", sentinel.read_bytes())

    def test_zip_validation_rejects_malformed_traversal_symlink_and_device_entries(self):
        cases = []

        malformed = self.backup_dir / "malformed.zip"
        malformed.write_bytes(b"not a zip")
        cases.append((malformed, "File is not a zip file"))

        traversal = self.backup_dir / "traversal.zip"
        self.write_zip(traversal, [("../outside/sentinel", b"changed")])
        cases.append((traversal, "traverses"))

        absolute = self.backup_dir / "absolute.zip"
        self.write_zip(absolute, [("C:\\outside\\sentinel", b"changed")])
        cases.append((absolute, "absolute"))

        symlink = self.backup_dir / "symlink.zip"
        symlink_entry = zipfile.ZipInfo("link")
        symlink_entry.create_system = 3
        symlink_entry.external_attr = (stat.S_IFLNK | 0o777) << 16
        self.write_zip(symlink, [("ignored", symlink_entry)])
        cases.append((symlink, "symlink"))

        device = self.backup_dir / "device.zip"
        device_entry = zipfile.ZipInfo("device")
        device_entry.create_system = 3
        device_entry.external_attr = (stat.S_IFCHR | 0o600) << 16
        self.write_zip(device, [("ignored", device_entry)])
        cases.append((device, "special-device"))

        manager = self.manager()
        artifacts = {item.filename: item for item in manager.inventory()}
        for path, reason in cases:
            with self.subTest(path=path.name):
                self.assertEqual("invalid", artifacts[path.name].validation.state)
                self.assertIn(reason, artifacts[path.name].validation.reason)
        self.assertFalse((self.root / "outside" / "sentinel").exists())

    def test_zip_validation_enforces_entry_and_metadata_bounds(self):
        archive = self.backup_dir / "bounded.zip"
        self.write_zip(archive, [("one", b"1"), ("two", b"2")])

        too_many = self.manager(max_entries=1).inventory()[0]
        too_much_metadata = self.manager(max_metadata_bytes=2).inventory()[0]

        self.assertEqual("invalid", too_many.validation.state)
        self.assertIn("entry count", too_many.validation.reason)
        self.assertEqual("invalid", too_much_metadata.validation.state)
        self.assertIn("metadata", too_much_metadata.validation.reason)

    def test_tar_gz_validation_accepts_regular_files_and_rejects_unsafe_types(self):
        valid = self.backup_dir / "valid.tar.gz"
        self.write_tar(valid, [("level.dat", b"restorable")])

        traversal = self.backup_dir / "traversal.tar.gz"
        self.write_tar(traversal, [("../outside/sentinel", b"changed")])

        symlink = self.backup_dir / "symlink.tar.gz"
        symlink_entry = tarfile.TarInfo("link")
        symlink_entry.type = tarfile.SYMTYPE
        symlink_entry.linkname = "../outside/sentinel"
        self.write_tar(symlink, [(symlink_entry, b"")])

        device = self.backup_dir / "device.tar.gz"
        device_entry = tarfile.TarInfo("device")
        device_entry.type = tarfile.CHRTYPE
        self.write_tar(device, [(device_entry, b"")])

        malformed = self.backup_dir / "malformed.tar.gz"
        malformed.write_bytes(b"not a tar archive")

        artifacts = {
            item.filename: item
            for item in self.manager(backups.ArchiveType.TAR_GZ).inventory()
        }

        self.assertEqual("valid", artifacts[valid.name].validation.state)
        self.assertEqual(1, artifacts[valid.name].validation.entry_count)
        for path, reason in (
            (traversal, "traverses"),
            (symlink, "link"),
            (device, "special-device"),
            (malformed, "archive"),
        ):
            with self.subTest(path=path.name):
                self.assertEqual("invalid", artifacts[path.name].validation.state)
                self.assertIn(reason, artifacts[path.name].validation.reason.lower())
        self.assertFalse((self.root / "outside" / "sentinel").exists())

    def test_verify_created_requires_exactly_one_new_nonempty_valid_source_artifact(self):
        manager = self.manager()
        before = manager.inventory()
        self.write_zip(self.backup_dir / "created.zip", [("level.dat", b"new")])
        after = manager.inventory()

        created = backups.verify_created(
            before, after, source_identity="minecraft:world"
        )

        self.assertEqual("created.zip", created.filename)
        with self.assertRaisesRegex(backups.BackupVerificationError, "exactly one"):
            backups.verify_created(after, after, source_identity="minecraft:world")
        with self.assertRaisesRegex(backups.BackupVerificationError, "source identity"):
            backups.verify_created(
                before,
                (replace(after[0], source_identity="other:world"),),
                source_identity="minecraft:world",
            )

        self.write_zip(self.backup_dir / "second.zip", [("level.dat", b"second")])
        with self.assertRaisesRegex(backups.BackupVerificationError, "exactly one"):
            backups.verify_created(
                before,
                manager.inventory(),
                source_identity="minecraft:world",
            )

    def test_create_backup_writes_and_verifies_zip_and_tar_gz(self):
        (self.source / "region").mkdir()
        (self.source / "region" / "r.0.0.mca").write_bytes(b"region-data")

        for archive_type in (backups.ArchiveType.ZIP, backups.ArchiveType.TAR_GZ):
            with self.subTest(archive_type=archive_type):
                for existing in self.backup_dir.iterdir():
                    existing.unlink()
                manager = self.manager(archive_type)

                artifact = manager.create_backup(label="manual")

                self.assertEqual("valid", artifact.validation.state)
                self.assertEqual("minecraft:world", artifact.source_identity)
                self.assertIn("manual", artifact.filename)
                self.assertTrue(artifact.filename.endswith(archive_type.suffix))
                self.assertEqual(artifact, manager.inventory()[0])

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_create_backup_rejects_source_symlink_swap_without_reading_outside(self):
        manager = self.manager()
        original_source = self.project / "original-world"
        self.source.rename(original_source)
        outside = self.root / "outside-world"
        outside.mkdir()
        sentinel = outside / "sentinel"
        sentinel.write_bytes(b"outside-secret")
        self.source.symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(backups.ConfinementError, "symlink"):
            manager.create_backup(label="unsafe")

        self.assertEqual(b"outside-secret", sentinel.read_bytes())
        self.assertEqual((), manager.inventory())

    def test_restore_preview_and_execute_refuse_running_server_and_wrong_confirmation(self):
        self.write_zip(self.backup_dir / "restore.zip", [("level.dat", b"restored")])
        manager = self.manager()
        artifact = manager.inventory()[0]

        with self.assertRaisesRegex(backups.ServerStateError, "stopped"):
            manager.preview_restore(
                artifact.artifact_id, server_state=backups.ServerState.RUNNING
            )

        preview = manager.preview_restore(
            artifact.artifact_id, server_state=backups.ServerState.STOPPED
        )
        self.assertEqual(("level.dat",), preview.archive_entries)
        self.assertIn("world/level.dat", preview.will_replace)
        self.assertEqual("minecraft:world", preview.source_identity)

        with self.assertRaisesRegex(backups.ServerStateError, "stopped"):
            manager.execute_restore(
                preview.preview_id,
                confirmation=preview.required_confirmation,
                server_state=backups.ServerState.RUNNING,
            )
        with self.assertRaisesRegex(backups.ConfirmationError, "confirmation"):
            manager.execute_restore(
                preview.preview_id,
                confirmation="RESTORE something else",
                server_state=backups.ServerState.STOPPED,
            )

        self.assertEqual(b"original-world", (self.source / "level.dat").read_bytes())
        self.assertEqual(1, len(manager.inventory()))

    def test_restore_stops_before_mutation_when_pre_restore_backup_fails(self):
        self.write_zip(self.backup_dir / "restore.zip", [("level.dat", b"restored")])
        manager = self.manager()
        artifact = manager.inventory()[0]
        preview = manager.preview_restore(
            artifact.artifact_id, server_state=backups.ServerState.STOPPED
        )
        sentinel = self.root / "outside-sentinel"
        sentinel.write_bytes(b"outside")

        with mock.patch.object(
            manager,
            "create_backup",
            side_effect=backups.BackupError("injected backup failure"),
        ), self.assertRaisesRegex(backups.RestoreError, "safety backup"):
            manager.execute_restore(
                preview.preview_id,
                confirmation=preview.required_confirmation,
                server_state=backups.ServerState.STOPPED,
            )

        self.assertEqual(b"original-world", (self.source / "level.dat").read_bytes())
        self.assertEqual(b"outside", sentinel.read_bytes())
        self.assertEqual(1, len(manager.inventory()))

    @unittest.skipUnless(os.name == "posix", "POSIX mode preservation test")
    def test_successful_restore_is_confined_preserves_mode_and_creates_safety_backup(self):
        archive_path = self.backup_dir / "restore.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            level = zipfile.ZipInfo("level.dat")
            level.create_system = 3
            level.external_attr = (stat.S_IFREG | 0o640) << 16
            archive.writestr(level, b"restored-world")
            nested = zipfile.ZipInfo("region/r.0.0.mca")
            nested.create_system = 3
            nested.external_attr = (stat.S_IFREG | 0o600) << 16
            archive.writestr(nested, b"restored-region")
        manager = self.manager()
        artifact = manager.inventory()[0]
        outside = self.root / "outside-sentinel"
        outside.write_bytes(b"outside-must-not-change")

        preview = manager.preview_restore(
            artifact.artifact_id, server_state=backups.ServerState.STOPPED
        )
        result = manager.execute_restore(
            preview.preview_id,
            confirmation=preview.required_confirmation,
            server_state=backups.ServerState.STOPPED,
        )

        self.assertEqual(artifact.artifact_id, result.artifact_id)
        self.assertEqual("minecraft:world", result.source_identity)
        self.assertTrue(result.safety_backup.validation.valid)
        self.assertEqual(tuple(sorted(preview.archive_entries)), result.restored_entries)
        self.assertEqual(b"restored-world", (self.source / "level.dat").read_bytes())
        self.assertEqual(
            b"restored-region", (self.source / "region" / "r.0.0.mca").read_bytes()
        )
        self.assertEqual(0o640, stat.S_IMODE((self.source / "level.dat").stat().st_mode))
        self.assertEqual(b"outside-must-not-change", outside.read_bytes())
        self.assertEqual(2, len(manager.inventory()))
        self.assertEqual([], list(self.project.glob(".ghc-restore-*")))
        self.assertEqual([], list(self.project.glob(".world.pre-restore-*")))

    def test_failed_post_replace_verification_restores_original_source(self):
        self.write_zip(self.backup_dir / "restore.zip", [("level.dat", b"restored")])
        manager = self.manager()
        artifact = manager.inventory()[0]
        preview = manager.preview_restore(
            artifact.artifact_id, server_state=backups.ServerState.STOPPED
        )
        outside = self.root / "outside-sentinel"
        outside.write_bytes(b"outside")

        with mock.patch.object(
            manager,
            "_verify_restored_source",
            side_effect=backups.RestoreError("injected readability failure"),
        ), self.assertRaisesRegex(backups.RestoreError, "restored original"):
            manager.execute_restore(
                preview.preview_id,
                confirmation=preview.required_confirmation,
                server_state=backups.ServerState.STOPPED,
            )

        self.assertEqual(b"original-world", (self.source / "level.dat").read_bytes())
        self.assertEqual(b"outside", outside.read_bytes())
        self.assertEqual([], list(self.project.glob(".ghc-restore-*")))
        self.assertEqual([], list(self.project.glob(".world.pre-restore-*")))

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_restore_revalidates_source_symlink_components_after_preview(self):
        self.write_zip(self.backup_dir / "restore.zip", [("level.dat", b"restored")])
        manager = self.manager()
        artifact = manager.inventory()[0]
        preview = manager.preview_restore(
            artifact.artifact_id, server_state=backups.ServerState.STOPPED
        )
        original_source = self.project / "original-world"
        self.source.rename(original_source)
        outside = self.root / "outside-world"
        outside.mkdir()
        sentinel = outside / "sentinel"
        sentinel.write_bytes(b"outside")
        self.source.symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(backups.ConfinementError, "symlink"):
            manager.execute_restore(
                preview.preview_id,
                confirmation=preview.required_confirmation,
                server_state=backups.ServerState.STOPPED,
            )

        self.assertEqual(b"outside", sentinel.read_bytes())
        self.assertEqual(b"original-world", (original_source / "level.dat").read_bytes())


if __name__ == "__main__":
    unittest.main()
