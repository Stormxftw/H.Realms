import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import diagnostics


FIXED_TIME = datetime(2026, 7, 29, 12, 30, tzinfo=timezone.utc)


class DiagnosticsCollectorPathTests(unittest.TestCase):
    def test_approved_relative_log_can_be_tailed_without_exposing_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "game"
            (root / "logs").mkdir(parents=True)
            (root / "logs" / "server.log").write_text(
                "starting\nready\n", encoding="utf-8"
            )
            collector = diagnostics.DiagnosticsCollector(
                root,
                {"server": "logs/server.log"},
                clock=lambda: FIXED_TIME,
            )

            result = collector.tail("server")

            self.assertEqual("server", result["logId"])
            self.assertEqual("ok", result["state"])
            self.assertEqual("starting\nready", result["content"])
            self.assertEqual("2026-07-29T12:30:00Z", result["collectedAt"])
            self.assertEqual(15, result["sizeBytes"])
            self.assertIsNotNone(result["modifiedAt"])
            self.assertFalse(result["truncated"])
            self.assertNotIn(str(root), repr(result))
            self.assertNotIn("logs/server.log", repr(result))

    def test_constructor_rejects_absolute_and_traversing_approved_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for relative in ("/var/log/private.log", "../outside.log", "logs/../../x"):
                with self.subTest(relative=relative), self.assertRaises(
                    diagnostics.DiagnosticsPathError
                ):
                    diagnostics.DiagnosticsCollector(root, {"server": relative})

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_constructor_rejects_symlink_components(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "game"
            outside = Path(tmp) / "outside"
            outside.mkdir()
            root.mkdir()
            (root / "logs").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(
                diagnostics.DiagnosticsPathError, "symlink component"
            ):
                diagnostics.DiagnosticsCollector(
                    root, {"server": "logs/server.log"}
                )

    def test_tail_rejects_arbitrary_request_identifier(self):
        with tempfile.TemporaryDirectory() as tmp:
            collector = diagnostics.DiagnosticsCollector(
                Path(tmp), {"server": "logs/server.log"}
            )

            with self.assertRaisesRegex(
                diagnostics.UnknownLogError, "log identifier is not approved"
            ) as raised:
                collector.tail("../outside.log")

            self.assertNotIn(str(Path(tmp)), str(raised.exception))


class DiagnosticsCollectorTailTests(unittest.TestCase):
    def test_missing_empty_and_binary_logs_have_explicit_safe_states(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "empty.log").touch()
            (root / "binary.log").write_bytes(b"valid-looking\x00secret-bytes\xff")
            collector = diagnostics.DiagnosticsCollector(
                root,
                {
                    "missing": "missing.log",
                    "empty": "empty.log",
                    "binary": "binary.log",
                },
                clock=lambda: FIXED_TIME,
            )

            missing = collector.tail("missing")
            empty = collector.tail("empty")
            binary = collector.tail("binary")

            self.assertEqual("missing", missing["state"])
            self.assertIsNone(missing["sizeBytes"])
            self.assertEqual("empty", empty["state"])
            self.assertEqual(0, empty["sizeBytes"])
            self.assertEqual("binary", binary["state"])
            self.assertEqual("", binary["content"])
            self.assertFalse(binary["truncated"])

    def test_huge_log_is_read_from_end_with_hard_byte_and_line_caps(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_path = root / "huge.log"
            log_path.write_bytes(
                b"unread-prefix\n" + b"x" * 1_000_000 + b"\nkeep-1\nkeep-2\nkeep-3\n"
            )
            collector = diagnostics.DiagnosticsCollector(
                root, {"server": "huge.log"}, max_bytes=128, max_lines=2
            )
            real_pread = os.pread
            reads = []

            def recording_pread(fd, count, offset):
                reads.append((count, offset))
                return real_pread(fd, count, offset)

            with mock.patch("diagnostics.os.pread", side_effect=recording_pread):
                result = collector.tail(
                    "server", max_bytes=100_000, max_lines=10_000
                )

            self.assertEqual("too_large", result["state"])
            self.assertEqual("keep-2\nkeep-3", result["content"])
            self.assertTrue(result["truncated"])
            self.assertEqual({"maxBytes": 128, "maxLines": 2}, result["limits"])
            self.assertEqual(1, len(reads))
            self.assertLessEqual(reads[0][0], 129)
            self.assertGreater(reads[0][1], 0)

    def test_control_characters_are_removed_and_secrets_are_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "server.log").write_bytes(
                b"\x1b[31mboom\x1b[0m\ttoken=abc123 password: 'hunter2'\n"
                b"Authorization: Bearer bearer-secret\n"
                b"X-API-Key: api-key-secret\n"
                b"LAN 192.168.1.25\r\n"
            )
            collector = diagnostics.DiagnosticsCollector(
                root, {"server": "server.log"}
            )

            default = collector.tail("server")
            with_ip_redaction = collector.tail("server", redact_ips=True)

            for secret in ("abc123", "hunter2", "bearer-secret", "api-key-secret"):
                self.assertNotIn(secret, default["content"])
            self.assertNotIn("\x1b", default["content"])
            self.assertNotIn("\t", default["content"])
            self.assertIn("[REDACTED]", default["content"])
            self.assertIn("192.168.1.25", default["content"])
            self.assertNotIn("192.168.1.25", with_ip_redaction["content"])
            self.assertIn("[REDACTED_IP]", with_ip_redaction["content"])

    def test_permission_denied_returns_safe_state_without_private_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "server.log").write_text("secret", encoding="utf-8")
            collector = diagnostics.DiagnosticsCollector(
                root, {"server": "server.log"}
            )

            with mock.patch(
                "diagnostics.DiagnosticsCollector._open_approved",
                side_effect=PermissionError("private path denied"),
            ):
                result = collector.tail("server")

            self.assertEqual("permission_denied", result["state"])
            self.assertEqual("", result["content"])
            self.assertNotIn(str(root), repr(result))
            self.assertNotIn("private path denied", repr(result))

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_symlink_introduced_after_construction_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "game"
            outside = Path(tmp) / "outside"
            root.mkdir()
            outside.mkdir()
            collector = diagnostics.DiagnosticsCollector(
                root, {"server": "logs/server.log"}
            )
            (root / "logs").symlink_to(outside, target_is_directory=True)
            (outside / "server.log").write_text("outside-secret", encoding="utf-8")

            result = collector.tail("server")

            self.assertEqual("unsafe", result["state"])
            self.assertEqual("", result["content"])
            self.assertNotIn("outside-secret", repr(result))

    def test_rotation_during_read_returns_rotated_without_stale_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_path = root / "server.log"
            rotated_path = root / "server.log.1"
            log_path.write_text("old-process-output\n", encoding="utf-8")
            collector = diagnostics.DiagnosticsCollector(
                root, {"server": "server.log"}
            )
            real_pread = os.pread

            def rotate_then_read(fd, count, offset):
                payload = real_pread(fd, count, offset)
                log_path.replace(rotated_path)
                log_path.write_text("new-process-output\n", encoding="utf-8")
                return payload

            with mock.patch("diagnostics.os.pread", side_effect=rotate_then_read):
                result = collector.tail("server")

            self.assertEqual("rotated", result["state"])
            self.assertEqual("", result["content"])
            self.assertFalse(result["truncated"])


class DiagnosticsBundleTests(unittest.TestCase):
    def test_bundle_contains_only_bounded_redacted_summaries_and_approved_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "server.log").write_text(
                "launch failed token=tail-secret at 192.168.1.25\n",
                encoding="utf-8",
            )
            collector = diagnostics.DiagnosticsCollector(
                root, {"server": "server.log"}, clock=lambda: FIXED_TIME
            )
            approved_tail = collector.tail("server")

            bundle = collector.build_bundle(
                version="0.3.0",
                readiness_blockers=[
                    {"code": "script_missing", "message": "password=blocker-secret"}
                ],
                capabilities={
                    "canStart": False,
                    "canViewLogs": True,
                    "config": "raw-config-must-not-appear",
                },
                telemetry_probe_errors=[
                    {
                        "probe": "a2s",
                        "message": "timeout at 192.168.1.25 Bearer probe-secret",
                    }
                ],
                active_operation={
                    "operationId": "op-active",
                    "action": "service.start",
                    "state": "failed",
                    "recoveryNote": f"inspect {root}/private token=operation-secret",
                    "output": "full-command-output-must-not-appear",
                    "command": ["sh", "unsafe-command-must-not-appear"],
                },
                recent_operations=[
                    {
                        "operationId": "op-recent",
                        "action": "service.stop",
                        "state": "succeeded",
                        "output": "recent-full-output-must-not-appear",
                    }
                ],
                log_tails=[
                    approved_tail,
                    {
                        "logId": "unapproved",
                        "state": "ok",
                        "content": "outside-log-must-not-appear",
                    },
                ],
            )

            for expected in (
                "Hermes Game Host Console diagnostics",
                "Version: 0.3.0",
                "[Readiness blockers]",
                "script_missing",
                "[Capabilities]",
                "canStart: false",
                "canViewLogs: true",
                "[Telemetry probe errors]",
                "a2s",
                "[Active operation]",
                "op-active",
                "service.start",
                "[Recent operations]",
                "op-recent",
                "[Approved log tails]",
                "server",
                "launch failed",
            ):
                self.assertIn(expected, bundle)
            for forbidden in (
                "blocker-secret",
                "probe-secret",
                "operation-secret",
                "tail-secret",
                "raw-config-must-not-appear",
                "full-command-output-must-not-appear",
                "unsafe-command-must-not-appear",
                "recent-full-output-must-not-appear",
                "outside-log-must-not-appear",
                str(root),
            ):
                self.assertNotIn(forbidden, bundle)
            self.assertIn("[REDACTED]", bundle)
            self.assertIn("[PRIVATE_PATH]", bundle)
            self.assertIn("192.168.1.25", bundle)

    def test_bundle_optionally_redacts_ips(self):
        with tempfile.TemporaryDirectory() as tmp:
            collector = diagnostics.DiagnosticsCollector(Path(tmp), {})

            bundle = collector.build_bundle(
                version="1",
                telemetry_probe_errors=["probe failed at 10.0.0.8"],
                redact_ips=True,
            )

            self.assertNotIn("10.0.0.8", bundle)
            self.assertIn("[REDACTED_IP]", bundle)

    def test_bundle_truncation_is_deterministic_and_utf8_byte_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            collector = diagnostics.DiagnosticsCollector(
                Path(tmp), {}, bundle_max_bytes=240
            )
            arguments = {
                "version": "版本-1",
                "readiness_blockers": ["blocked-" + "é" * 500],
                "capabilities": {"canStart": False},
            }

            first = collector.build_bundle(**arguments)
            second = collector.build_bundle(**arguments)

            self.assertEqual(first, second)
            self.assertLessEqual(len(first.encode("utf-8")), 240)
            self.assertTrue(first.endswith("\n...[diagnostics truncated]\n"))

    def test_standalone_redaction_covers_json_options_and_token_shapes(self):
        source = (
            "{\"api_key\": \"json-secret\"} --password option-secret "
            "Bearer bearer-secret ghp_abcdefghijklmnopqrstuvwxyz123456 "
            "config=/home/operator/private/server.ini"
        )

        redacted = diagnostics.redact_text(source, redact_private_paths=True)

        for secret in (
            "json-secret",
            "option-secret",
            "bearer-secret",
            "ghp_abcdefghijklmnopqrstuvwxyz123456",
            "/home/operator/private/server.ini",
        ):
            self.assertNotIn(secret, redacted)
        self.assertGreaterEqual(redacted.count("[REDACTED]"), 4)
        self.assertIn("[PRIVATE_PATH]", redacted)


if __name__ == "__main__":
    unittest.main()
