import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import restart_state


class RestartStateStoreTests(unittest.TestCase):
    def test_default_store_path_uses_xdg_state_home(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"XDG_STATE_HOME": tmp}
        ):
            store = restart_state.RestartStateStore()

            self.assertEqual(
                Path(tmp) / "hermes-game-host-console" / "restart-state.json",
                store.path,
            )

    def test_pending_change_survives_reopen_with_unknown_effective_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state" / "restart-state.json"
            store = restart_state.RestartStateStore(path)
            store.record_change(
                game_id="minecraft",
                control_id="difficulty",
                configured_value="hard",
                originating_operation_id="op-001",
                changed_at="2026-07-29T12:00:00Z",
            )

            pending = restart_state.RestartStateStore(path).list_pending("minecraft")

            self.assertEqual(1, len(pending))
            self.assertEqual("minecraft", pending[0]["gameId"])
            self.assertEqual("difficulty", pending[0]["controlId"])
            self.assertEqual("hard", pending[0]["configuredValue"])
            self.assertIsNone(pending[0]["effectiveValue"])
            self.assertFalse(pending[0]["effectiveValueKnown"])
            self.assertEqual("unknown", pending[0]["effectiveValueStatus"])
            self.assertEqual("op-001", pending[0]["originatingOperationId"])
            self.assertEqual("2026-07-29T12:00:00Z", pending[0]["changedAt"])
            self.assertTrue(pending[0]["restartRequired"])

    @unittest.skipUnless(os.name == "posix", "POSIX mode bits are not available")
    def test_store_secures_parent_and_file_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "private-state" / "restart-state.json"

            restart_state.RestartStateStore(path).record_change(
                game_id="minecraft",
                control_id="motd",
                configured_value="Hermes",
                originating_operation_id="op-permissions",
                changed_at="2026-07-29T12:00:00Z",
            )

            self.assertEqual(0o700, stat.S_IMODE(path.parent.stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))

    def test_failed_atomic_replace_preserves_store_and_outside_sentinel(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "private-state" / "restart-state.json"
            sentinel = root / "outside" / "sentinel.bin"
            sentinel.parent.mkdir()
            sentinel.write_bytes(b"outside-must-not-change")
            store = restart_state.RestartStateStore(path)
            store.record_change(
                game_id="minecraft",
                control_id="difficulty",
                configured_value="normal",
                originating_operation_id="op-before",
                changed_at="2026-07-29T12:00:00Z",
            )
            original_store = path.read_bytes()

            with mock.patch(
                "restart_state.os.replace", side_effect=OSError("replace blocked")
            ), self.assertRaisesRegex(
                restart_state.RestartStateError, "could not atomically persist"
            ):
                store.record_change(
                    game_id="minecraft",
                    control_id="difficulty",
                    configured_value="hard",
                    originating_operation_id="op-after",
                    changed_at="2026-07-29T12:01:00Z",
                )

            self.assertEqual(original_store, path.read_bytes())
            self.assertEqual(b"outside-must-not-change", sentinel.read_bytes())
            self.assertEqual([], list(path.parent.glob(f".{path.name}.*.tmp")))

    def test_malformed_json_fails_closed_without_overwriting_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "restart-state.json"
            malformed = b'{"schemaVersion": 1, "games":'
            path.write_bytes(malformed)

            with self.assertRaisesRegex(
                restart_state.RestartStateCorruptionError, "invalid JSON"
            ):
                restart_state.RestartStateStore(path).record_change(
                    game_id="minecraft",
                    control_id="difficulty",
                    configured_value="hard",
                    originating_operation_id="op-corrupt",
                    changed_at="2026-07-29T12:00:00Z",
                )

            self.assertEqual(malformed, path.read_bytes())

    def test_structurally_invalid_state_fails_closed_without_overwriting_state(self):
        invalid_states = (
            [],
            {"schemaVersion": 2, "games": {}},
            {"schemaVersion": 1, "games": []},
            {"schemaVersion": 1, "games": {"minecraft": {"controls": []}}},
            {
                "schemaVersion": 1,
                "games": {
                    "minecraft": {
                        "controls": {"difficulty": {"restartRequired": True}}
                    }
                },
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "restart-state.json"
            for index, invalid_state in enumerate(invalid_states):
                with self.subTest(index=index):
                    payload = (json.dumps(invalid_state) + "\n").encode()
                    path.write_bytes(payload)

                    with self.assertRaisesRegex(
                        restart_state.RestartStateCorruptionError,
                        "structurally invalid",
                    ):
                        restart_state.RestartStateStore(path).record_change(
                            game_id="minecraft",
                            control_id="difficulty",
                            configured_value="hard",
                            originating_operation_id="op-invalid",
                            changed_at="2026-07-29T12:00:00Z",
                        )

                    self.assertEqual(payload, path.read_bytes())

    def test_repeated_edit_upserts_latest_value_and_preserves_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = restart_state.RestartStateStore(Path(tmp) / "state.json")
            store.record_change(
                game_id="minecraft",
                control_id="difficulty",
                configured_value="normal",
                originating_operation_id="op-first",
                changed_at="2026-07-29T12:00:00Z",
            )
            store.record_change(
                game_id="minecraft",
                control_id="difficulty",
                configured_value="hard",
                originating_operation_id="op-latest",
                changed_at="2026-07-29T12:05:00Z",
            )

            pending = store.list_pending("minecraft")

            self.assertEqual(1, len(pending))
            self.assertEqual("hard", pending[0]["configuredValue"])
            self.assertEqual("op-latest", pending[0]["originatingOperationId"])
            self.assertEqual("op-first", pending[0]["firstOperationId"])
            self.assertEqual("op-latest", pending[0]["latestOperationId"])
            self.assertEqual("2026-07-29T12:00:00Z", pending[0]["firstChangedAt"])
            self.assertEqual("2026-07-29T12:05:00Z", pending[0]["changedAt"])
            self.assertEqual(
                [
                    {
                        "configuredValue": "normal",
                        "operationId": "op-first",
                        "changedAt": "2026-07-29T12:00:00Z",
                    },
                    {
                        "configuredValue": "hard",
                        "operationId": "op-latest",
                        "changedAt": "2026-07-29T12:05:00Z",
                    },
                ],
                pending[0]["history"],
            )

    def test_nullable_effective_value_is_distinct_from_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = restart_state.RestartStateStore(Path(tmp) / "state.json")

            store.record_change(
                game_id="minecraft",
                control_id="optional-setting",
                configured_value="configured",
                effective_value=None,
                originating_operation_id="op-known-null",
                changed_at="2026-07-29T12:00:00Z",
            )

            pending = store.list_pending("minecraft")[0]
            self.assertIsNone(pending["effectiveValue"])
            self.assertTrue(pending["effectiveValueKnown"])
            self.assertEqual("known", pending["effectiveValueStatus"])

    def test_verified_transition_clears_only_changes_at_or_before_finish(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            store = restart_state.RestartStateStore(path)
            for game_id, control_id, changed_at in (
                ("minecraft", "old", "2026-07-29T12:00:00Z"),
                ("minecraft", "equal", "2026-07-29T12:05:00Z"),
                ("minecraft", "new", "2026-07-29T12:10:00Z"),
                ("palworld", "other-game", "2026-07-29T11:00:00Z"),
            ):
                store.record_change(
                    game_id=game_id,
                    control_id=control_id,
                    configured_value=control_id,
                    originating_operation_id=f"op-{control_id}",
                    changed_at=changed_at,
                )

            cleared = store.mark_verified_transition(
                "minecraft", "2026-07-29T12:05:00Z"
            )

            self.assertEqual(["equal", "old"], cleared)
            reopened = restart_state.RestartStateStore(path)
            self.assertEqual(
                ["new"],
                [item["controlId"] for item in reopened.list_pending("minecraft")],
            )
            self.assertEqual(
                ["other-game"],
                [item["controlId"] for item in reopened.list_pending("palworld")],
            )

    def test_failed_transition_does_not_clear_pending_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            store = restart_state.RestartStateStore(path)
            store.record_change(
                game_id="minecraft",
                control_id="difficulty",
                configured_value="hard",
                originating_operation_id="op-change",
                changed_at="2026-07-29T12:00:00Z",
            )

            cleared = store.mark_verified_transition(
                "minecraft", "2026-07-29T13:00:00Z", successful=False
            )

            self.assertEqual([], cleared)
            self.assertEqual(
                ["difficulty"],
                [
                    item["controlId"]
                    for item in restart_state.RestartStateStore(path).list_pending(
                        "minecraft"
                    )
                ],
            )

    def test_pending_restart_presentation_hides_banner_without_pending_changes(self):
        presentation = restart_state.pending_restart_presentation(
            "running", pending_count=0
        )

        self.assertEqual(
            {
                "pendingCount": 0,
                "bannerState": "none",
                "showBanner": False,
                "canOfferRestart": False,
                "bannerText": "",
                "nextActionText": "",
            },
            presentation,
        )

    def test_pending_restart_presentation_guides_unknown_status_safely(self):
        presentation = restart_state.pending_restart_presentation(
            "unknown", pending_count=1
        )

        self.assertEqual("pending-unknown", presentation["bannerState"])
        self.assertTrue(presentation["showBanner"])
        self.assertFalse(presentation["canOfferRestart"])
        self.assertIn("Verify server status", presentation["nextActionText"])
        self.assertIn("verified start or restart", presentation["nextActionText"])

    def test_pending_restart_presentation_covers_running_stopped_and_degraded(self):
        running = restart_state.pending_restart_presentation("running", pending_count=2)
        stopped = restart_state.pending_restart_presentation("stopped", pending_count=1)
        degraded = restart_state.pending_restart_presentation("degraded", pending_count=1)

        self.assertEqual("restart-required", running["bannerState"])
        self.assertTrue(running["showBanner"])
        self.assertTrue(running["canOfferRestart"])
        self.assertIn("Restart the running server", running["nextActionText"])

        self.assertEqual("pending-next-start", stopped["bannerState"])
        self.assertEqual(
            "1 pending change requires a verified transition.", stopped["bannerText"]
        )
        self.assertFalse(stopped["canOfferRestart"])
        self.assertIn("next verified start", stopped["nextActionText"])

        self.assertEqual("pending-degraded", degraded["bannerState"])
        self.assertFalse(degraded["canOfferRestart"])
        self.assertIn("verify", degraded["nextActionText"].lower())
        self.assertIn("start or restart", degraded["nextActionText"].lower())


if __name__ == "__main__":
    unittest.main()
