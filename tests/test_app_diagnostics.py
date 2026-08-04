"""Endpoint-surface tests for the diagnostics wiring.

Covers the pure helpers that back the diagnostics routes:
  - GET /api/diagnostics/<gameId>/logs          -> approved log ids
  - GET /api/diagnostics/<gameId>/logs/<logId>  -> bounded, redacted tail
  - GET /api/diagnostics/<gameId>/bundle        -> redacted diagnostics bundle

These are read-only; safety lives in path confinement, bounded reads, and
IP/redaction defaults, not in the plan/apply mutation model.
"""

import json
import tempfile
import unittest
from pathlib import Path

import app_diagnostics
import control_engine


def _write_project_with_logs(root: Path) -> Path:
    project = root / "projects" / "palworld"
    logs = project / "logs"
    logs.mkdir(parents=True)
    (logs / "palworld-linux.log").write_text(
        "boot line 1\nboot line 2\nplayer 10.0.0.99 connected\nlast line\n",
        encoding="utf-8",
    )
    (logs / "empty.log").write_text("", encoding="utf-8")
    return project


def _engine(root: Path) -> control_engine.ControlEngine:
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
    return control_engine.ControlEngine(
        projects_root=root / "projects",
        profiles_dir=profiles,
        audit_path=root / "audit.jsonl",
        adapter_config_path=root / "game_adapters.json",
    )


class DiagnosticsLogsTests(unittest.TestCase):
    def test_logs_lists_approved_ids_for_installed_game(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_project_with_logs(root)
            engine = _engine(root)
            result = app_diagnostics.list_diagnostics_logs(engine, "palworld")
            self.assertEqual("palworld", result["gameId"])
            self.assertIn("server", result["logs"])
            self.assertEqual(
                result["logs"]["server"],
                "logs/palworld-linux.log",
            )

    def test_logs_unknown_game_raises_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_project_with_logs(root)
            engine = _engine(root)
            with self.assertRaises(app_diagnostics.DiagnosticsNotFoundError):
                app_diagnostics.list_diagnostics_logs(engine, "nope")


class DiagnosticsTailTests(unittest.TestCase):
    def test_tail_returns_bounded_content_default_redaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_project_with_logs(root)
            engine = _engine(root)
            result = app_diagnostics.diagnostics_log_tail(engine, "palworld", "server")
            self.assertEqual("ok", result["state"])
            self.assertEqual("server", result["logId"])
            self.assertIn("last line", result["content"])
            # IPs redacted by default for the read-only console surface.
            self.assertNotIn("10.0.0.99", result["content"])

    def test_tail_respects_explicit_redact_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_project_with_logs(root)
            engine = _engine(root)
            result = app_diagnostics.diagnostics_log_tail(
                engine, "palworld", "server", redact_ips=False
            )
            self.assertIn("10.0.0.99", result["content"])

    def test_tail_unapproved_log_id_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_project_with_logs(root)
            engine = _engine(root)
            with self.assertRaises(app_diagnostics.DiagnosticsNotFoundError):
                app_diagnostics.diagnostics_log_tail(
                    engine, "palworld", "../../etc/passwd"
                )
            with self.assertRaises(app_diagnostics.DiagnosticsNotFoundError):
                app_diagnostics.diagnostics_log_tail(engine, "palworld", "missing")


class DiagnosticsBundleTests(unittest.TestCase):
    def test_bundle_is_redacted_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_project_with_logs(root)
            engine = _engine(root)
            bundle = app_diagnostics.diagnostics_bundle(engine, "palworld")
            self.assertIsInstance(bundle, str)
            self.assertNotIn("10.0.0.99", bundle)


if __name__ == "__main__":
    unittest.main()
