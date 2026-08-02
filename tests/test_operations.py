import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from operations import InvalidTransitionError, OperationStore, OperationStoreError


class OperationStoreTests(unittest.TestCase):
    def test_operation_persists_across_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state" / "operations.db"
            created = OperationStore(db_path=db_path).create(
                game_id="minecraft",
                action="service.start",
                actor="operator",
                source="desktop",
                precondition={"running": False},
            )

            reopened = OperationStore(db_path=db_path)

            self.assertEqual(created, reopened.get(created["operationId"]))
            self.assertEqual("queued", created["state"])
            self.assertEqual({"running": False}, created["precondition"])

    def test_transition_records_lifecycle_timestamps_and_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = OperationStore(db_path=Path(tmp) / "operations.db")
            created = store.create(
                game_id="minecraft",
                action="service.start",
                actor="operator",
                source="http",
            )

            running = store.transition(created["operationId"], "running")
            failed = store.transition(
                created["operationId"],
                "failed",
                output="process exited 1",
                postcondition={"running": False},
                recovery_note="start was rejected",
            )

            self.assertIsNotNone(running["startedAt"])
            self.assertIsNone(running["finishedAt"])
            self.assertEqual("failed", failed["state"])
            self.assertEqual(running["startedAt"], failed["startedAt"])
            self.assertIsNotNone(failed["finishedAt"])
            self.assertEqual("process exited 1", failed["output"])
            self.assertEqual({"running": False}, failed["postcondition"])
            self.assertEqual("start was rejected", failed["recoveryNote"])

    def test_list_filters_by_game_and_state_and_honors_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = OperationStore(db_path=Path(tmp) / "operations.db")
            first = store.create(
                game_id="alpha", action="service.start", actor="a", source="test"
            )
            store.transition(first["operationId"], "failed")
            store.create(game_id="alpha", action="service.stop", actor="a", source="test")
            store.create(game_id="beta", action="service.start", actor="a", source="test")

            records = store.list(limit=1, game_id="alpha", state="failed")

            self.assertEqual([first["operationId"]], [item["operationId"] for item in records])

    def test_recover_interrupted_cancels_queued_and_marks_running_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = OperationStore(db_path=Path(tmp) / "operations.db")
            running = store.create(
                game_id="alpha", action="service.start", actor="a", source="test"
            )
            queued = store.create(
                game_id="beta", action="backup.create", actor="a", source="test"
            )
            store.transition(running["operationId"], "running")

            recovered = store.recover_interrupted("host process restarted")

            self.assertEqual(2, recovered)
            unknown = store.get(running["operationId"])
            self.assertEqual("outcome_unknown", unknown["state"])
            self.assertIsNotNone(unknown["finishedAt"])
            self.assertEqual("host process restarted", unknown["recoveryNote"])
            cancelled = store.get(queued["operationId"])
            self.assertEqual("cancelled", cancelled["state"])
            self.assertIsNotNone(cancelled["finishedAt"])
            self.assertIn("before execution", cancelled["recoveryNote"])

    def test_sensitive_text_is_redacted_and_output_is_deterministically_truncated(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "operations.db"
            store = OperationStore(db_path=db_path, output_limit=160)
            project_root = Path(__file__).parents[1]
            created = store.create(
                game_id="alpha",
                action="service.start",
                actor="a",
                source="test",
                precondition={
                    "api_key": "condition-secret",
                    "config": str(Path.home() / "private" / "config.ini"),
                },
            )
            raw_output = (
                "token=token-secret password='password-secret' "
                f"home={Path.home()}/private project={project_root}/data "
                + "x" * 500
            )

            failed = store.transition(
                created["operationId"],
                "failed",
                output=raw_output,
                recovery_note="secret=recovery-secret",
            )

            self.assertLessEqual(len(failed["output"]), 160)
            self.assertTrue(failed["output"].endswith("...[truncated]"))
            with sqlite3.connect(db_path) as connection:
                stored = " ".join(
                    value or ""
                    for value in connection.execute(
                        "SELECT output, precondition_json, recovery_note FROM operations"
                    ).fetchone()
                )
            for sensitive in (
                "token-secret",
                "password-secret",
                "condition-secret",
                "recovery-secret",
                str(Path.home()),
                str(project_root),
            ):
                self.assertNotIn(sensitive, stored)
            self.assertIn("[REDACTED]", stored)

    def test_create_can_record_a_rejected_outcome_directly(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = OperationStore(db_path=Path(tmp) / "operations.db")

            rejected = store.create(
                game_id="alpha",
                action="service.start",
                actor="a",
                source="http",
                state="failed",
                output="request rejected by precondition",
                postcondition={"running": False},
                recovery_note="no command was launched",
            )

            self.assertEqual("failed", rejected["state"])
            self.assertIsNone(rejected["startedAt"])
            self.assertIsNotNone(rejected["finishedAt"])
            self.assertEqual("request rejected by precondition", rejected["output"])

    def test_retention_prunes_only_old_terminal_rows_in_bounded_batches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
            store = OperationStore(
                db_path=root / "state" / "operations.db", clock=lambda: now[0]
            )
            for action in ("service.start", "service.stop"):
                store.create(
                    game_id="alpha",
                    action=action,
                    actor="a",
                    source="test",
                    state="failed",
                )
            running = store.create(
                game_id="alpha", action="backup.create", actor="a", source="test"
            )
            store.transition(running["operationId"], "running")
            game_save = root / "game-data" / "world.save"
            backup = root / "backups" / "world.zip"
            game_save.parent.mkdir()
            backup.parent.mkdir()
            game_save.write_text("save", encoding="utf-8")
            backup.write_text("backup", encoding="utf-8")
            now[0] += timedelta(days=31)

            pruned = store.prune(retention_days=30, batch_limit=1)

            self.assertEqual(1, pruned)
            self.assertEqual(1, len(store.list(state="failed")))
            self.assertEqual("running", store.get(running["operationId"])["state"])
            self.assertEqual("save", game_save.read_text(encoding="utf-8"))
            self.assertEqual("backup", backup.read_text(encoding="utf-8"))

    def test_failed_migration_rolls_back_and_blocks_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "operations.db"
            with sqlite3.connect(db_path) as connection:
                connection.execute("CREATE TABLE legacy (value TEXT NOT NULL)")
                connection.execute("INSERT INTO legacy VALUES ('preserved')")
                connection.commit()
            broken_schema = root / "broken-schema.sql"
            broken_schema.write_text(
                """
                CREATE TABLE operations (operation_id TEXT PRIMARY KEY);
                PRAGMA user_version = 1;
                COMMIT;
                THIS IS NOT SQL;
                """,
                encoding="utf-8",
            )

            with self.assertRaisesRegex(OperationStoreError, r"migration.*failed.*writes blocked"):
                OperationStore(db_path=db_path, schema_path=broken_schema)

            with sqlite3.connect(db_path) as connection:
                self.assertEqual(
                    "preserved", connection.execute("SELECT value FROM legacy").fetchone()[0]
                )
                self.assertEqual(0, connection.execute("PRAGMA user_version").fetchone()[0])
                table_names = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            self.assertEqual({"legacy"}, table_names)


if __name__ == "__main__":
    unittest.main()
