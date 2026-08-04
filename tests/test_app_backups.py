"""Endpoint-surface tests for the backup wiring.

Covers the pure helpers that back the backup routes:
  - GET  /api/backups/<gameId>                 -> artifact inventory
  - POST /api/backups/<gameId>/create          -> one verified archive
  - POST /api/backups/<gameId>/restore/preview -> restore preview + token

Restore *execute* is intentionally not exposed here — it is the one path that
overwrites live data and is deferred until it is deliberately warranted.
"""

import json
import tempfile
import unittest
from pathlib import Path

import app_backups
import control_engine


def _write_game_tree(root: Path) -> None:
    """Provision a palworld game with live server state (the backup source)."""
    project = root / "projects" / "palworld"
    saved = project / "Pal" / "Saved"
    saved.mkdir(parents=True)
    (saved / "world.dat").write_text("world-bytes", encoding="utf-8")
    (saved / "config.ini").write_text("[Server]\nname=Strugglebus\n", encoding="utf-8")

    profiles = root / "profiles"
    profiles.mkdir(parents=True)
    (profiles / "palworld.json").write_text(
        json.dumps(
            {
                "schemaVersion": "1.0",
                "id": "palworld",
                "name": "Palworld",
                "controls": [],
            }
        ),
        encoding="utf-8",
    )
    (root / "game_adapters.json").write_text(
        json.dumps(
            {
                "games": {
                    "palworld": {
                        "projectDir": "palworld",
                        "commands": {},
                        "processSearch": "PalServer",
                        "defaultPort": 8211,
                        "portProtocol": "udp",
                    }
                }
            }
        ),
        encoding="utf-8",
    )


def _engine(root: Path) -> control_engine.ControlEngine:
    return control_engine.ControlEngine(
        projects_root=root / "projects",
        profiles_dir=root / "profiles",
        audit_path=root / "audit.jsonl",
        adapter_config_path=root / "game_adapters.json",
    )


class BackupListTests(unittest.TestCase):
    def test_list_empty_inventory_for_installed_game(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_game_tree(root)
            engine = _engine(root)
            result = app_backups.list_backups(engine, "palworld")
            self.assertEqual("palworld", result["gameId"])
            self.assertEqual([], result["backups"])

    def test_list_unknown_game_raises_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_game_tree(root)
            engine = _engine(root)
            with self.assertRaises(app_backups.BackupNotFoundError):
                app_backups.list_backups(engine, "nope")


class BackupCreateTests(unittest.TestCase):
    def test_create_produces_one_valid_artifact_then_lists_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_game_tree(root)
            engine = _engine(root)
            created = app_backups.create_backup(engine, "palworld")
            self.assertTrue(created["filename"].endswith(".tar.gz"))
            self.assertEqual("valid", created["validation"]["state"])
            self.assertGreater(created["sizeBytes"], 0)
            # The new artifact is now present in the inventory.
            inventory = app_backups.list_backups(engine, "palworld")["backups"]
            self.assertEqual(1, len(inventory))
            self.assertEqual(created["artifactId"], inventory[0]["artifactId"])

    def test_create_label_is_sanitised(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_game_tree(root)
            engine = _engine(root)
            created = app_backups.create_backup(
                engine, "palworld", label="Pre Update!! v2"
            )
            self.assertIn("pre-update-v2", created["filename"])


class BackupRestorePreviewTests(unittest.TestCase):
    def test_restore_preview_returns_entries_and_confirmation_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_game_tree(root)
            engine = _engine(root)
            created = app_backups.create_backup(engine, "palworld")
            preview = app_backups.preview_restore(
                engine, "palworld", created["artifactId"], server_state="stopped"
            )
            self.assertEqual(created["artifactId"], preview["artifactId"])
            self.assertIn("RESTORE", preview["requiredConfirmation"])
            self.assertIn(created["artifactId"], preview["requiredConfirmation"])
            self.assertTrue(preview["previewId"].startswith("restore-"))
            self.assertGreater(len(preview["archiveEntries"]), 0)

    def test_restore_preview_unknown_artifact_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_game_tree(root)
            engine = _engine(root)
            app_backups.create_backup(engine, "palworld")
            with self.assertRaises(app_backups.BackupNotFoundError):
                app_backups.preview_restore(
                    engine, "palworld", "does-not-exist", server_state="stopped"
                )


if __name__ == "__main__":
    unittest.main()
