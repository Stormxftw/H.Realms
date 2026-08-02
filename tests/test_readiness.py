import json
import tempfile
import unittest
from pathlib import Path

from control_engine import ControlEngine, ControlEngineError


CAPABILITY_KEYS = {
    "canStart",
    "canStop",
    "canRestart",
    "canConfigure",
    "canBackup",
    "canRestore",
    "canViewLogs",
}


def write_game(
    root: Path,
    *,
    adapter_overrides=None,
    controls=None,
    create_project=False,
):
    projects = root / "projects"
    profiles = root / "profiles"
    projects.mkdir()
    profiles.mkdir()
    adapter = {
        "projectDir": "alpha-server",
        "commands": {
            "service.start": [["start.sh", 60]],
            "service.stop": [["stop.sh", 60]],
            "service.restart": [["stop.sh", 60], ["start.sh", 60]],
        },
        "propertyTypes": {},
        "statusCollector": "process_only",
        "processSearch": "alpha-server",
        "defaultPort": 7777,
        "portProtocol": "tcp",
    }
    adapter.update(adapter_overrides or {})
    (root / "game_adapters.json").write_text(
        json.dumps({"games": {"alpha": adapter}}), encoding="utf-8"
    )
    controls = controls if controls is not None else [
        {
            "id": "start",
            "kind": "button",
            "label": "Start server",
            "risk": "service",
            "enabledWhen": "offline",
            "binding": {"action": "service.start"},
        },
        {
            "id": "stop",
            "kind": "button",
            "label": "Stop server",
            "risk": "disruptive",
            "enabledWhen": "online",
            "binding": {"action": "service.stop"},
        },
    ]
    (profiles / "alpha.json").write_text(
        json.dumps(
            {
                "schemaVersion": "1.0",
                "id": "alpha",
                "name": "Alpha",
                "controls": controls,
            }
        ),
        encoding="utf-8",
    )
    project = projects / "alpha-server"
    if create_project:
        project.mkdir()
        for name in ("start.sh", "stop.sh"):
            script = project / name
            script.write_text("#!/bin/sh\n", encoding="utf-8")
            script.chmod(0o755)
    engine = ControlEngine(
        projects_root=projects,
        profiles_dir=profiles,
        audit_path=root / "audit.jsonl",
    )
    return engine, project


class ReadinessTests(unittest.TestCase):
    def test_absent_catalog_game_needs_setup_and_disables_every_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, _project = write_game(Path(tmp))

            game = engine.catalog()["games"][0]

            self.assertEqual("needs_setup", game["readiness"])
            self.assertEqual("project_missing", game["blockers"][0]["code"])
            self.assertEqual(CAPABILITY_KEYS, set(game["capabilities"]))
            self.assertTrue(all(value is False for value in game["capabilities"].values()))
            self.assertEqual(CAPABILITY_KEYS, set(game["capabilityReasons"]))
            mutation_controls = [
                control
                for control in game["controls"]
                if control["binding"]["action"] != "ui.refresh"
            ]
            self.assertTrue(mutation_controls)
            self.assertTrue(all(control["disabled"] is True for control in mutation_controls))
            self.assertTrue(all(control["disabledReason"] for control in mutation_controls))

            with self.assertRaisesRegex(ControlEngineError, "action unavailable.*project directory"):
                engine.plan(
                    game_id="alpha",
                    control_id="start",
                    value=None,
                    actor="bypass-test",
                )

    def test_required_binary_config_and_world_evidence_drive_readiness(self):
        controls = [
            {
                "id": "start",
                "kind": "button",
                "label": "Start server",
                "risk": "service",
                "binding": {"action": "service.start"},
            },
            {
                "id": "server-name",
                "kind": "text",
                "label": "Server name",
                "risk": "configuration",
                "binding": {"action": "property.set", "key": "server-name"},
            },
        ]
        required_paths = [
            {"path": "alpha-server.bin", "kind": "binary", "type": "file"},
            {"path": "server.properties", "kind": "config", "type": "file"},
            {"path": "world", "kind": "world", "type": "directory"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine, project = write_game(
                root,
                create_project=True,
                controls=controls,
                adapter_overrides={
                    "propertyTypes": {"server-name": "string"},
                    "requiredPaths": required_paths,
                },
            )

            partial = engine.game_view("alpha")

            # requiredPaths are advisory hints, not gates: with action scripts
            # present, Start is enabled even when the canonical binary/config/save
            # paths are absent. The config control stays gated until a real
            # server.properties exists (the operational contract).
            self.assertEqual("misconfigured", partial["readiness"])
            self.assertEqual(
                {"capability_misconfigured"},
                {blocker["code"] for blocker in partial["blockers"]},
            )
            self.assertTrue(partial["capabilities"]["canStart"])
            self.assertFalse(partial["capabilities"]["canConfigure"])
            self.assertEqual(
                {"binary_hint", "config_hint", "world_hint"},
                {hint["code"] for hint in partial["hints"]},
            )

            (project / "alpha-server.bin").write_bytes(b"server")
            (project / "server.properties").write_text(
                "server-name=Alpha\n", encoding="utf-8"
            )
            (project / "world").mkdir()

            ready = engine.game_view("alpha")
            self.assertEqual("ready", ready["readiness"])
            self.assertEqual([], ready["blockers"])
            self.assertEqual([], ready["hints"])
            self.assertTrue(ready["capabilities"]["canStart"])
            self.assertTrue(ready["capabilities"]["canConfigure"])

    def test_required_paths_are_advisory_for_running_external_server(self):
        # Palworld-style: the project dir holds only the action scripts, while the
        # real binary/save lives outside it. The console must still allow Start/Stop
        # based on the operational contract (scripts present + executable), and must
        # surface the canonical layout as non-blocking hints.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine, _project = write_game(
                root,
                create_project=True,
                adapter_overrides={
                    "requiredPaths": [
                        {"path": "Pal/Binaries/Linux/PalServer", "kind": "binary", "type": "file"},
                        {"path": "Pal/Saved/SaveGames", "kind": "save", "type": "directory"},
                    ]
                },
            )
            view = engine.game_view("alpha")
            self.assertEqual("ready", view["readiness"])
            self.assertTrue(view["capabilities"]["canStart"])
            self.assertTrue(view["capabilities"]["canStop"])
            self.assertEqual([], view["blockers"])
            hint_codes = {hint["code"] for hint in view["hints"]}
            self.assertIn("binary_hint", hint_codes)
            self.assertIn("save_hint", hint_codes)

    def test_present_project_with_broken_approved_script_is_misconfigured(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, project = write_game(Path(tmp), create_project=True)
            (project / "start.sh").chmod(0o644)

            game = engine.game_view("alpha")

            self.assertEqual("misconfigured", game["readiness"])
            self.assertFalse(game["capabilities"]["canStart"])
            self.assertIn("executable", game["capabilityReasons"]["canStart"])

    def test_lifecycle_action_rejects_switch_control_at_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ControlEngineError, "lifecycle.*button"):
                write_game(
                    Path(tmp),
                    create_project=True,
                    controls=[
                        {
                            "id": "start",
                            "kind": "switch",
                            "label": "Start",
                            "risk": "service",
                            "binding": {"action": "service.start"},
                        }
                    ],
                )

    def test_integer_property_rejects_nonintegral_number_before_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine, project = write_game(
                root,
                create_project=True,
                adapter_overrides={"propertyTypes": {"slots": "integer"}},
                controls=[
                    {
                        "id": "slots",
                        "kind": "number",
                        "label": "Slots",
                        "risk": "configuration",
                        "binding": {"action": "property.set", "key": "slots"},
                    }
                ],
            )
            properties = project / "server.properties"
            properties.write_text("slots=4\n", encoding="utf-8")

            with self.assertRaisesRegex(ControlEngineError, "whole number"):
                engine.plan(
                    game_id="alpha",
                    control_id="slots",
                    value=4.5,
                    actor="integer-test",
                )
            self.assertEqual("slots=4\n", properties.read_text(encoding="utf-8"))

    def test_select_requires_at_least_one_option_at_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ControlEngineError, "select.*option"):
                write_game(
                    Path(tmp),
                    create_project=True,
                    adapter_overrides={"propertyTypes": {"mode": "string"}},
                    controls=[
                        {
                            "id": "mode",
                            "kind": "select",
                            "label": "Mode",
                            "risk": "configuration",
                            "options": [],
                            "binding": {"action": "property.set", "key": "mode"},
                        }
                    ],
                )


if __name__ == "__main__":
    unittest.main()
