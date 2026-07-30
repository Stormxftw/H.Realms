import copy
import json
import tempfile
import unittest
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from pathlib import Path

from control_engine import (
    ControlEngine,
    ControlEngineError,
    OperationConflictError,
    OperationRejectedError,
)
from operations import OperationStore
from restart_state import RestartStateStore


class MutableClock:
    def __init__(self):
        self.now = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
        self.monotonic_value = 0.0

    def utcnow(self):
        return self.now

    def monotonic(self):
        return self.monotonic_value

    def sleep(self, seconds):
        self.monotonic_value += seconds
        self.now += timedelta(seconds=seconds)

    def advance(self, seconds):
        self.sleep(seconds)


class FakeStatusProvider:
    def __init__(self, *, state="stopped", running=False, pid=None, ok=True):
        self.set(state=state, running=running, pid=pid, ok=ok)
        self.calls = 0

    def set(self, *, state, running, pid=None, ok=True, error=None):
        self.value = {
            "state": state,
            "online": running is True,
            "process": {
                "ok": ok,
                "running": running,
                "pid": pid,
                "error": error,
                "detector": "adapter-processSearch:server.jar nogui",
            },
        }

    def __call__(self, game_id):
        self.calls += 1
        result = copy.deepcopy(self.value)
        result["id"] = game_id
        return result


class ManualExecutor:
    def __init__(self):
        self.tasks = []

    def submit(self, function, *args):
        future = Future()
        self.tasks.append((future, function, args))
        return future

    def run_next(self):
        future, function, args = self.tasks.pop(0)
        try:
            future.set_result(function(*args))
        except Exception as exc:
            future.set_exception(exc)
        return future.result()


def write_control_tree(root: Path):
    projects = root / "projects"
    profiles = root / "profiles"
    project = projects / "minecraft-server"
    project.mkdir(parents=True)
    profiles.mkdir()
    for name in ("start.sh", "stop.sh"):
        script = project / name
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        script.chmod(0o755)
    (project / "server.properties").write_text("max-players=10\n", encoding="utf-8")
    adapter_path = root / "game_adapters.json"
    adapter_path.write_text(
        json.dumps(
            {
                "games": {
                    "minecraft": {
                        "projectDir": "minecraft-server",
                        "commands": {
                            "service.start": [["start.sh", 60]],
                            "service.stop": [["stop.sh", 60]],
                            "service.restart": [["stop.sh", 60], ["start.sh", 60]],
                        },
                        "propertyTypes": {"max-players": "integer"},
                        "statusCollector": "process_only",
                        "processSearch": "server.jar nogui",
                        "defaultPort": 25565,
                        "portProtocol": "tcp",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (profiles / "minecraft.json").write_text(
        json.dumps(
            {
                "schemaVersion": "1.0",
                "id": "minecraft",
                "name": "Minecraft Java",
                "controls": [
                    {
                        "id": "start",
                        "kind": "button",
                        "label": "Start",
                        "risk": "service",
                        "binding": {"action": "service.start"},
                    },
                    {
                        "id": "stop",
                        "kind": "button",
                        "label": "Stop",
                        "risk": "disruptive",
                        "binding": {"action": "service.stop"},
                    },
                    {
                        "id": "restart",
                        "kind": "button",
                        "label": "Restart",
                        "risk": "disruptive",
                        "binding": {"action": "service.restart"},
                    },
                    {
                        "id": "max-players",
                        "kind": "number",
                        "label": "Maximum players",
                        "risk": "configuration",
                        "restartRequired": True,
                        "min": 1,
                        "max": 50,
                        "step": 1,
                        "binding": {"action": "property.set", "key": "max-players"},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return projects, profiles, adapter_path


class LifecycleOperationTests(unittest.TestCase):
    def make_engine(self, root, status, *, runner=None, clock=None, **kwargs):
        projects, profiles, adapter_path = write_control_tree(root)
        clock = clock or MutableClock()
        store = OperationStore(db_path=root / "state" / "operations.db", clock=clock.utcnow)
        restart_store = RestartStateStore(root / "state" / "restart-state.json")
        engine = ControlEngine(
            projects_root=projects,
            profiles_dir=profiles,
            audit_path=root / "audit.jsonl",
            adapter_config_path=adapter_path,
            command_runner=runner or (lambda *_args, **_kwargs: {"ok": True, "exitCode": 0, "output": "ok"}),
            operation_store=store,
            restart_state_store=restart_store,
            status_provider=status,
            clock=clock.utcnow,
            monotonic=clock.monotonic,
            sleeper=clock.sleep,
            **kwargs,
        )
        self.addCleanup(engine.close)
        return engine, store, restart_store, clock

    def test_plan_binds_profile_and_status_and_expires_at_fixed_ttl(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status = FakeStatusProvider()
            engine, _store, _restart, clock = self.make_engine(
                root, status, plan_ttl_seconds=5
            )

            plan = engine.plan(
                game_id="minecraft", control_id="start", value=None, actor="operator"
            )

            self.assertRegex(plan["profileDigest"], r"^[0-9a-f]{64}$")
            self.assertRegex(plan["statusFingerprint"], r"^[0-9a-f]{64}$")
            self.assertEqual(clock.now.timestamp() + 5, plan["expiresAt"])
            clock.advance(6)
            with self.assertRaisesRegex(ControlEngineError, "plan expired"):
                engine.apply(
                    plan_id=plan["planId"],
                    plan_digest=plan["planDigest"],
                    actor="operator",
                    confirmed=True,
                    source="test",
                )

    def test_pending_plan_count_is_bounded_and_oldest_plan_is_purged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine, _store, _restart, _clock = self.make_engine(
                root, FakeStatusProvider(), max_pending_plans=2
            )
            first = engine.plan(
                game_id="minecraft", control_id="start", value=None, actor="operator"
            )
            engine.plan(
                game_id="minecraft", control_id="stop", value=None, actor="operator"
            )
            engine.plan(
                game_id="minecraft", control_id="restart", value=None, actor="operator"
            )

            with self.assertRaisesRegex(ControlEngineError, "unknown or already used plan"):
                engine.apply(
                    plan_id=first["planId"],
                    plan_digest=first["planDigest"],
                    actor="operator",
                    confirmed=True,
                    source="test",
                )

    def test_start_while_running_and_stop_while_stopped_are_durable_idempotent_successes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = []

            def runner(*args, **kwargs):
                calls.append((args, kwargs))
                return {"ok": True, "exitCode": 0, "output": "must not run"}

            status = FakeStatusProvider(state="running_ready", running=True, pid=123)
            engine, store, _restart, _clock = self.make_engine(
                root, status, runner=runner
            )
            start_plan = engine.plan(
                game_id="minecraft", control_id="start", value=None, actor="operator"
            )
            started = engine.apply(
                plan_id=start_plan["planId"],
                plan_digest=start_plan["planDigest"],
                actor="operator",
                confirmed=True,
                source="desktop",
            )

            status.set(state="stopped", running=False)
            stop_plan = engine.plan(
                game_id="minecraft", control_id="stop", value=None, actor="operator"
            )
            stopped = engine.apply(
                plan_id=stop_plan["planId"],
                plan_digest=stop_plan["planDigest"],
                actor="operator",
                confirmed=True,
                source="desktop",
            )

            self.assertEqual([], calls)
            for result in (started, stopped):
                self.assertEqual("succeeded", result["state"])
                self.assertTrue(result["postcondition"]["verified"])
                self.assertTrue(result["postcondition"]["idempotent"])
                self.assertEqual(result, store.get(result["operationId"]))

    def test_degraded_lifecycle_precondition_is_rejected_and_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = []
            status = FakeStatusProvider(
                state="running_degraded", running=True, pid=123, ok=True
            )
            engine, store, _restart, _clock = self.make_engine(
                root,
                status,
                runner=lambda *args, **kwargs: calls.append((args, kwargs)),
            )
            plan = engine.plan(
                game_id="minecraft", control_id="stop", value=None, actor="operator"
            )

            with self.assertRaisesRegex(OperationRejectedError, "unsafe lifecycle precondition") as rejected:
                engine.apply(
                    plan_id=plan["planId"],
                    plan_digest=plan["planDigest"],
                    actor="operator",
                    confirmed=True,
                    source="bridge",
                )

            operation = rejected.exception.operation
            self.assertEqual("failed", operation["state"])
            self.assertEqual("bridge", operation["source"])
            self.assertIn("no command", operation["recoveryNote"])
            self.assertEqual(operation, store.get(operation["operationId"]))
            self.assertEqual([], calls)

    def test_submit_is_queued_and_conflict_names_active_operation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executor = ManualExecutor()
            status = FakeStatusProvider()

            def runner(*_args, **_kwargs):
                status.set(state="running_ready", running=True, pid=321)
                return {"ok": True, "exitCode": 0, "output": "started"}

            engine, store, _restart, _clock = self.make_engine(
                root, status, runner=runner, executor=executor
            )
            first = engine.plan(
                game_id="minecraft", control_id="start", value=None, actor="operator"
            )
            second = engine.plan(
                game_id="minecraft", control_id="start", value=None, actor="operator"
            )

            queued = engine.submit_apply(
                plan_id=first["planId"],
                plan_digest=first["planDigest"],
                actor="operator",
                confirmed=True,
                source="http",
            )
            self.assertEqual("queued", queued["state"])
            with self.assertRaises(OperationConflictError) as conflict:
                engine.submit_apply(
                    plan_id=second["planId"],
                    plan_digest=second["planDigest"],
                    actor="operator",
                    confirmed=True,
                    source="http",
                )
            self.assertEqual(queued["operationId"], conflict.exception.active_operation_id)

            completed = executor.run_next()
            self.assertEqual("succeeded", completed["state"])
            self.assertEqual(completed, store.get(queued["operationId"]))

    def test_status_change_after_preview_is_durably_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = []
            status = FakeStatusProvider()
            engine, store, _restart, _clock = self.make_engine(
                root,
                status,
                runner=lambda *args, **kwargs: calls.append((args, kwargs)),
            )
            plan = engine.plan(
                game_id="minecraft", control_id="start", value=None, actor="operator"
            )
            status.set(state="running_ready", running=True, pid=99)

            with self.assertRaisesRegex(OperationRejectedError, "status changed") as rejected:
                engine.apply(
                    plan_id=plan["planId"],
                    plan_digest=plan["planDigest"],
                    actor="operator",
                    confirmed=True,
                )

            self.assertEqual("failed", rejected.exception.operation["state"])
            self.assertEqual([], calls)
            self.assertEqual(
                rejected.exception.operation,
                store.get(rejected.exception.operation["operationId"]),
            )

    def test_profile_digest_change_after_preview_is_durably_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = []
            engine, _store, _restart, _clock = self.make_engine(
                root,
                FakeStatusProvider(),
                runner=lambda *args, **kwargs: calls.append((args, kwargs)),
            )
            plan = engine.plan(
                game_id="minecraft", control_id="start", value=None, actor="operator"
            )
            engine._registry._profiles["minecraft"]["name"] = "Reloaded profile"

            with self.assertRaisesRegex(OperationRejectedError, "profile changed"):
                engine.apply(
                    plan_id=plan["planId"],
                    plan_digest=plan["planDigest"],
                    actor="operator",
                    confirmed=True,
                )
            self.assertEqual([], calls)

    def test_exit_zero_with_false_postcondition_fails_after_bounded_polling(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status = FakeStatusProvider()
            engine, _store, _restart, clock = self.make_engine(
                root,
                status,
                postcondition_timeout=3,
                poll_interval=1,
            )
            plan = engine.plan(
                game_id="minecraft", control_id="start", value=None, actor="operator"
            )

            with self.assertRaisesRegex(OperationRejectedError, "postcondition") as rejected:
                engine.apply(
                    plan_id=plan["planId"],
                    plan_digest=plan["planDigest"],
                    actor="operator",
                    confirmed=True,
                )

            self.assertEqual(3, clock.monotonic_value)
            self.assertEqual("failed", rejected.exception.operation["state"])
            self.assertFalse(rejected.exception.operation["postcondition"]["verified"])

    def test_partial_restart_failure_records_recovery_guidance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status = FakeStatusProvider(state="running_ready", running=True, pid=12)

            def runner(argv, **_kwargs):
                if Path(argv[0]).name == "stop.sh":
                    return {"ok": True, "exitCode": 0, "output": "stopped"}
                return {"ok": False, "exitCode": 1, "output": "start failed"}

            engine, _store, _restart, _clock = self.make_engine(
                root, status, runner=runner
            )
            plan = engine.plan(
                game_id="minecraft", control_id="restart", value=None, actor="operator"
            )

            with self.assertRaises(OperationRejectedError) as rejected:
                engine.apply(
                    plan_id=plan["planId"],
                    plan_digest=plan["planDigest"],
                    actor="operator",
                    confirmed=True,
                )
            self.assertIn("partially completed", rejected.exception.operation["recoveryNote"])
            self.assertIn("may be stopped", rejected.exception.operation["recoveryNote"])

    def test_restart_required_property_is_recorded_then_cleared_by_verified_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status = FakeStatusProvider()

            def runner(*_args, **_kwargs):
                status.set(state="running_ready", running=True, pid=777)
                return {"ok": True, "exitCode": 0, "output": "started"}

            engine, _store, restart_store, _clock = self.make_engine(
                root, status, runner=runner
            )
            property_plan = engine.plan(
                game_id="minecraft",
                control_id="max-players",
                value=20,
                actor="operator",
            )
            property_result = engine.apply(
                plan_id=property_plan["planId"],
                plan_digest=property_plan["planDigest"],
                actor="operator",
                confirmed=True,
            )
            pending = restart_store.list_pending("minecraft")
            self.assertEqual(["max-players"], [item["controlId"] for item in pending])
            self.assertEqual(property_result["operationId"], pending[0]["originatingOperationId"])

            start_plan = engine.plan(
                game_id="minecraft", control_id="start", value=None, actor="operator"
            )
            started = engine.apply(
                plan_id=start_plan["planId"],
                plan_digest=start_plan["planDigest"],
                actor="operator",
                confirmed=True,
            )
            self.assertEqual("succeeded", started["state"])
            self.assertEqual([], restart_store.list_pending("minecraft"))


if __name__ == "__main__":
    unittest.main()
