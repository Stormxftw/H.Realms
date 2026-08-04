import json
import tempfile
import unittest
from pathlib import Path

from control_engine import ControlEngine
from operations import OperationStore
from restart_state import RestartStateStore


_MC_ADAPTER = {
    "games": {
        "minecraft": {
            "projectDir": "minecraft-server",
            "commands": {
                "service.start": [["start.sh", 60]],
                "service.stop": [["stop.sh", 120]],
                "service.restart": [["stop.sh", 120], ["start.sh", 60]],
                "backup.create": [["backup.sh", 300]],
            },
            "propertyTypes": {
                "difficulty": "string",
                "max-players": "integer",
                "white-list": "boolean",
                "view-distance": "integer",
                "motd": "string",
            },
            "statusCollector": "minecraft_ping",
            "processSearch": "server.jar nogui",
            "defaultPort": 25565,
            "portProtocol": "tcp",
        },
    },
}


def _create_executable_adapter_scripts(project_dir):
    scripts = {
        script
        for specs in _MC_ADAPTER["games"]["minecraft"]["commands"].values()
        for script, _timeout in specs
    }
    for script_name in scripts:
        script = project_dir / script_name
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        script.chmod(0o755)


class ControlEngineCatalogTests(unittest.TestCase):
    def test_checked_in_registry_loads_read_only_against_generic_projects_root(self):
        repository_root = Path(__file__).parents[1]
        audit_path = repository_root / "tests" / ".real-config-audit-must-not-exist.jsonl"
        self.assertFalse(audit_path.exists())

        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            projects_root = state_root / "projects"
            projects_root.mkdir()
            engine = ControlEngine(
                projects_root=projects_root,
                profiles_dir=repository_root / "game_profiles",
                audit_path=audit_path,
                adapter_config_path=repository_root / "game_adapters.json",
                operation_store=OperationStore(
                    db_path=state_root / "operations.db",
                    private_path_prefixes=(Path.home(), projects_root),
                ),
                restart_state_store=RestartStateStore(
                    state_root / "restart-state.json"
                ),
            )
            catalog = engine.catalog()

            game_ids = {game["id"] for game in catalog["games"]}
            self.assertEqual({"minecraft", "palworld"}, game_ids)
        self.assertFalse(audit_path.exists())
        self.assertFalse((repository_root / "tests" / "operations.db").exists())

    def test_catalog_game_view_and_plan_use_startup_profile_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            profiles = root / "profiles"
            minecraft = projects / "minecraft-server"
            minecraft.mkdir(parents=True)
            _create_executable_adapter_scripts(minecraft)
            profiles.mkdir()
            (root / "game_adapters.json").write_text(json.dumps(_MC_ADAPTER))
            profile_path = profiles / "minecraft.json"
            profile = {
                "schemaVersion": "1.0",
                "id": "minecraft",
                "name": "Validated Minecraft",
                "controls": [
                    {
                        "id": "backup-now",
                        "kind": "button",
                        "label": "Create backup",
                        "risk": "safe-mutation",
                        "binding": {"action": "backup.create"},
                    }
                ],
            }
            profile_path.write_text(json.dumps(profile), encoding="utf-8")
            calls = []

            def fake_runner(argv, *, cwd, timeout):
                calls.append((argv, cwd, timeout))
                return {"ok": True, "exitCode": 0, "output": "snapshot command"}

            engine = ControlEngine(
                projects_root=projects,
                profiles_dir=profiles,
                audit_path=root / "audit.jsonl",
                command_runner=fake_runner,
            )
            profile["name"] = "Unvalidated edit"
            profile["controls"][0]["risk"] = "read-only"
            profile["controls"][0]["binding"]["action"] = "service.start"
            profile_path.write_text(json.dumps(profile), encoding="utf-8")

            catalog = engine.catalog()
            view = engine.game_view("minecraft")
            plan = engine.plan(
                game_id="minecraft",
                control_id="backup-now",
                value=None,
                actor="snapshot-test",
            )

            self.assertEqual("Validated Minecraft", catalog["games"][0]["name"])
            self.assertEqual("Validated Minecraft", view["name"])
            self.assertEqual("backup.create", plan["action"])
            self.assertEqual("safe-mutation", plan["risk"])
            result = engine.apply(
                plan_id=plan["planId"],
                plan_digest=plan["planDigest"],
                actor="snapshot-test",
                confirmed=True,
            )
            self.assertTrue(result["ok"])
            self.assertEqual(
                [([str(minecraft / "backup.sh")], minecraft, 300)], calls
            )

    def test_catalog_keeps_validated_profiles_after_source_file_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            profiles = root / "profiles"
            minecraft = projects / "minecraft-server"
            minecraft.mkdir(parents=True)
            _create_executable_adapter_scripts(minecraft)
            profiles.mkdir()
            (root / "game_adapters.json").write_text(json.dumps(_MC_ADAPTER))
            profile_path = profiles / "minecraft.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": "1.0",
                        "id": "minecraft",
                        "name": "Minecraft Java",
                        "controls": [],
                    }
                ),
                encoding="utf-8",
            )
            engine = ControlEngine(
                projects_root=projects,
                profiles_dir=profiles,
                audit_path=root / "audit.jsonl",
            )
            profile_path.unlink()

            self.assertEqual(
                ["minecraft"], [game["id"] for game in engine.catalog()["games"]]
            )
            self.assertEqual("minecraft", engine.game_view("minecraft")["id"])

    def test_registry_and_game_views_return_defensive_copies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            profiles = root / "profiles"
            minecraft = projects / "minecraft-server"
            minecraft.mkdir(parents=True)
            _create_executable_adapter_scripts(minecraft)
            profiles.mkdir()
            (root / "game_adapters.json").write_text(json.dumps(_MC_ADAPTER))
            (minecraft / "server.properties").write_text(
                "max-players=12\n", encoding="utf-8"
            )
            (profiles / "minecraft.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": "1.0",
                        "id": "minecraft",
                        "name": "Minecraft Java",
                        "controls": [
                            {
                                "id": "max-players",
                                "kind": "slider",
                                "label": "Max players",
                                "risk": "configuration",
                                "min": 1,
                                "max": 50,
                                "step": 1,
                                "binding": {
                                    "action": "property.set",
                                    "key": "max-players",
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            engine = ControlEngine(
                projects_root=projects,
                profiles_dir=profiles,
                audit_path=root / "audit.jsonl",
            )

            adapters = engine._registry.adapters
            adapters["minecraft"]["propertyTypes"]["max-players"] = "boolean"
            profiles_copy = engine._registry.profiles
            profiles_copy["minecraft"]["controls"][0]["binding"]["key"] = "difficulty"
            first_view = engine.game_view("minecraft")
            first_view["controls"][0]["binding"]["key"] = "difficulty"

            second_view = engine.game_view("minecraft")
            plan = engine.plan(
                game_id="minecraft",
                control_id="max-players",
                value=20,
                actor="copy-test",
            )
            self.assertEqual(12, second_view["controls"][0]["value"])
            self.assertEqual("max-players", plan["controlId"])
            self.assertEqual(12, plan["currentValue"])

    def test_catalog_resolves_current_minecraft_property_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            profiles = root / "profiles"
            minecraft = projects / "minecraft-server"
            minecraft.mkdir(parents=True)
            _create_executable_adapter_scripts(minecraft)
            profiles.mkdir()
            (root / "game_adapters.json").write_text(json.dumps(_MC_ADAPTER))
            (minecraft / "server.properties").write_text(
                "difficulty=hard\nmax-players=12\nwhite-list=true\n",
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
                                "id": "difficulty",
                                "kind": "select",
                                "label": "Difficulty",
                                "risk": "configuration",
                                "binding": {
                                    "action": "property.set",
                                    "key": "difficulty",
                                },
                                "options": [
                                    {"value": "easy", "label": "Easy"},
                                    {"value": "normal", "label": "Normal"},
                                    {"value": "hard", "label": "Hard"},
                                ],
                            },
                            {
                                "id": "max-players",
                                "kind": "slider",
                                "label": "Max players",
                                "risk": "configuration",
                                "binding": {
                                    "action": "property.set",
                                    "key": "max-players",
                                },
                                "min": 1,
                                "max": 50,
                                "step": 1,
                            },
                            {
                                "id": "whitelist",
                                "kind": "switch",
                                "label": "Whitelist",
                                "risk": "configuration",
                                "binding": {
                                    "action": "property.set",
                                    "key": "white-list",
                                },
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            engine = ControlEngine(
                projects_root=projects,
                profiles_dir=profiles,
                audit_path=root / "audit.jsonl",
            )

            view = engine.game_view("minecraft")
            catalog = engine.catalog()

            values = {control["id"]: control["value"] for control in view["controls"]}
            self.assertEqual("hard", values["difficulty"])
            self.assertEqual(12, values["max-players"])
            self.assertIs(True, values["whitelist"])
            self.assertEqual("Minecraft Java", view["name"])
            self.assertEqual(["minecraft"], [game["id"] for game in catalog["games"]])

    def test_catalog_rejects_executable_or_unknown_control_kinds(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            profiles = root / "profiles"
            (projects / "minecraft-server").mkdir(parents=True)
            _create_executable_adapter_scripts(projects / "minecraft-server")
            profiles.mkdir()
            (root / "game_adapters.json").write_text(json.dumps(_MC_ADAPTER))
            (projects / "minecraft-server" / "server.properties").write_text(
                "difficulty=normal\n",
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
                                "id": "unsafe",
                                "kind": "script",
                                "label": "Run arbitrary code",
                                "risk": "read-only",
                                "binding": {"action": "ui.refresh"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                r"minecraft\.json: \$\.controls\[0\]\.kind: 'script' is not one of",
            ):
                ControlEngine(
                    projects_root=projects,
                    profiles_dir=profiles,
                    audit_path=root / "audit.jsonl",
                )

    def test_plan_validates_slider_and_does_not_mutate_properties(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            profiles = root / "profiles"
            minecraft = projects / "minecraft-server"
            minecraft.mkdir(parents=True)
            _create_executable_adapter_scripts(minecraft)
            profiles.mkdir()
            (root / "game_adapters.json").write_text(json.dumps(_MC_ADAPTER))
            properties = minecraft / "server.properties"
            properties.write_text("max-players=12\n", encoding="utf-8")
            (profiles / "minecraft.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": "1.0",
                        "id": "minecraft",
                        "name": "Minecraft Java",
                        "controls": [
                            {
                                "id": "max-players",
                                "kind": "slider",
                                "label": "Max players",
                                "risk": "configuration",
                                "restartRequired": True,
                                "min": 1,
                                "max": 50,
                                "step": 1,
                                "binding": {
                                    "action": "property.set",
                                    "key": "max-players",
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            engine = ControlEngine(
                projects_root=projects,
                profiles_dir=profiles,
                audit_path=root / "audit.jsonl",
            )

            plan = engine.plan(
                game_id="minecraft",
                control_id="max-players",
                value=20,
                actor="hermes",
            )

            self.assertTrue(plan["requiresConfirmation"])
            self.assertTrue(plan["restartRequired"])
            self.assertEqual(12, plan["currentValue"])
            self.assertEqual(20, plan["proposedValue"])
            self.assertEqual("property.set", plan["action"])
            self.assertTrue(plan["planId"])
            self.assertEqual("max-players=12\n", properties.read_text(encoding="utf-8"))

    def test_apply_requires_one_time_confirmation_and_writes_rollback_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            profiles = root / "profiles"
            minecraft = projects / "minecraft-server"
            minecraft.mkdir(parents=True)
            _create_executable_adapter_scripts(minecraft)
            profiles.mkdir()
            (root / "game_adapters.json").write_text(json.dumps(_MC_ADAPTER))
            properties = minecraft / "server.properties"
            properties.write_text("difficulty=normal\nmax-players=12\n", encoding="utf-8")
            audit_path = root / "audit.jsonl"
            (profiles / "minecraft.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": "1.0",
                        "id": "minecraft",
                        "name": "Minecraft Java",
                        "controls": [
                            {
                                "id": "max-players",
                                "kind": "slider",
                                "label": "Max players",
                                "risk": "configuration",
                                "restartRequired": True,
                                "min": 1,
                                "max": 50,
                                "step": 1,
                                "binding": {
                                    "action": "property.set",
                                    "key": "max-players",
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            engine = ControlEngine(
                projects_root=projects,
                profiles_dir=profiles,
                audit_path=audit_path,
            )
            plan = engine.plan(
                game_id="minecraft",
                control_id="max-players",
                value=20,
                actor="hermes",
            )

            with self.assertRaisesRegex(ValueError, "confirmation required"):
                engine.apply(plan_id=plan["planId"], actor="hermes", confirmed=False)
            self.assertEqual(
                "difficulty=normal\nmax-players=12\n",
                properties.read_text(encoding="utf-8"),
            )

            result = engine.apply(
                plan_id=plan["planId"],
                plan_digest=plan["planDigest"],
                actor="hermes",
                confirmed=True,
            )

            self.assertTrue(result["ok"])
            self.assertTrue(result["restartRequired"])
            self.assertEqual(
                "difficulty=normal\nmax-players=20\n",
                properties.read_text(encoding="utf-8"),
            )
            rollback = Path(result["rollbackPath"])
            self.assertTrue(rollback.is_file())
            self.assertEqual(
                "difficulty=normal\nmax-players=12\n",
                rollback.read_text(encoding="utf-8"),
            )
            audit = json.loads(audit_path.read_text(encoding="utf-8").strip())
            self.assertEqual("hermes", audit["actor"])
            self.assertEqual(12, audit["before"])
            self.assertEqual(20, audit["after"])
            with self.assertRaisesRegex(ValueError, "unknown or already used plan"):
                engine.apply(plan_id=plan["planId"], actor="hermes", confirmed=True)

    def test_button_actions_use_only_hardcoded_adapter_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            profiles = root / "profiles"
            minecraft = projects / "minecraft-server"
            minecraft.mkdir(parents=True)
            _create_executable_adapter_scripts(minecraft)
            profiles.mkdir()
            (root / "game_adapters.json").write_text(json.dumps(_MC_ADAPTER))
            (minecraft / "server.properties").write_text("difficulty=normal\n", encoding="utf-8")
            backup_script = minecraft / "backup.sh"
            backup_script.write_text("#!/bin/sh\n", encoding="utf-8")
            calls = []

            def fake_runner(argv, *, cwd, timeout):
                calls.append((argv, cwd, timeout))
                return {"ok": True, "exitCode": 0, "output": "backup created"}

            (profiles / "minecraft.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": "1.0",
                        "id": "minecraft",
                        "name": "Minecraft Java",
                        "controls": [
                            {
                                "id": "backup-now",
                                "kind": "button",
                                "label": "Create backup",
                                "risk": "safe-mutation",
                                "binding": {"action": "backup.create"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            engine = ControlEngine(
                projects_root=projects,
                profiles_dir=profiles,
                audit_path=root / "audit.jsonl",
                command_runner=fake_runner,
            )
            plan = engine.plan(
                game_id="minecraft",
                control_id="backup-now",
                value=None,
                actor="hermes",
            )

            result = engine.apply(
                plan_id=plan["planId"],
                plan_digest=plan["planDigest"],
                actor="hermes",
                confirmed=True,
            )

            self.assertTrue(result["ok"])
            self.assertEqual(
                [([str(backup_script)], minecraft, 300)],
                calls,
            )
            self.assertEqual("backup created", result["output"])

    def test_jsonl_audit_is_private_bounded_and_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            profiles = root / "profiles"
            minecraft = projects / "minecraft-server"
            minecraft.mkdir(parents=True)
            _create_executable_adapter_scripts(minecraft)
            profiles.mkdir()
            (root / "game_adapters.json").write_text(json.dumps(_MC_ADAPTER))
            (minecraft / "server.properties").write_text("difficulty=normal\n")
            (profiles / "minecraft.json").write_text(json.dumps({
                "schemaVersion": "1.0", "id": "minecraft", "name": "Minecraft",
                "controls": [{"id": "backup-now", "kind": "button", "label": "Backup",
                              "risk": "safe-mutation", "binding": {"action": "backup.create"}}],
            }))
            secret = "sk-1234567890abcdefghijklmnop"
            engine = ControlEngine(
                projects_root=projects, profiles_dir=profiles,
                audit_path=root / "private" / "control-audit.jsonl",
                command_runner=lambda *_args, **_kwargs: {
                    "ok": True, "exitCode": 0,
                    "output": "x" * 6000 + f" token={secret} Authorization: Bearer abcdef {Path.home()}/private",
                },
            )
            plan = engine.plan(game_id="minecraft", control_id="backup-now", value=None, actor="hermes")
            engine.apply(plan_id=plan["planId"], plan_digest=plan["planDigest"], actor="hermes", confirmed=True)

            audit_path = root / "private" / "control-audit.jsonl"
            stored = audit_path.read_text()
            self.assertNotIn(secret, stored)
            self.assertNotIn(str(Path.home()), stored)
            self.assertIn("[REDACTED]", stored)
            self.assertLess(len(stored), 6000)
            self.assertEqual(0o700, audit_path.parent.stat().st_mode & 0o777)
            self.assertEqual(0o600, audit_path.stat().st_mode & 0o777)

    def test_profile_rejects_unknown_fields_and_unsupported_schema_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            profiles = root / "profiles"
            (projects / "minecraft-server").mkdir(parents=True)
            _create_executable_adapter_scripts(projects / "minecraft-server")
            profiles.mkdir()
            (root / "game_adapters.json").write_text(json.dumps(_MC_ADAPTER))
            (projects / "minecraft-server" / "server.properties").write_text(
                "difficulty=normal\n",
                encoding="utf-8",
            )
            profile_path = profiles / "minecraft.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": "1.0",
                        "id": "minecraft",
                        "name": "Minecraft Java",
                        "controls": [
                            {
                                "id": "refresh",
                                "kind": "button",
                                "label": "Refresh",
                                "risk": "read-only",
                                "command": "whoami",
                                "binding": {"action": "ui.refresh"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ValueError,
                r"minecraft\.json: \$\.controls\[0\]: Additional properties are not allowed.*command",
            ):
                ControlEngine(
                    projects_root=projects,
                    profiles_dir=profiles,
                    audit_path=root / "audit.jsonl",
                )

            profile_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": "2.0",
                        "id": "minecraft",
                        "name": "Minecraft Java",
                        "controls": [],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ValueError,
                r"minecraft\.json: \$\.schemaVersion: '1\.0' was expected",
            ):
                ControlEngine(
                    projects_root=projects,
                    profiles_dir=profiles,
                    audit_path=root / "audit.jsonl",
                )

    def test_apply_is_bound_to_preview_actor_and_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            profiles = root / "profiles"
            minecraft = projects / "minecraft-server"
            minecraft.mkdir(parents=True)
            _create_executable_adapter_scripts(minecraft)
            profiles.mkdir()
            (root / "game_adapters.json").write_text(json.dumps(_MC_ADAPTER))
            (minecraft / "server.properties").write_text("difficulty=normal\n", encoding="utf-8")
            backup_script = minecraft / "backup.sh"
            backup_script.write_text("#!/bin/sh\n", encoding="utf-8")
            (profiles / "minecraft.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": "1.0",
                        "id": "minecraft",
                        "name": "Minecraft Java",
                        "controls": [
                            {
                                "id": "backup-now",
                                "kind": "button",
                                "label": "Create backup",
                                "risk": "safe-mutation",
                                "binding": {"action": "backup.create"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            engine = ControlEngine(
                projects_root=projects,
                profiles_dir=profiles,
                audit_path=root / "audit.jsonl",
                command_runner=lambda *args, **kwargs: {"ok": True, "exitCode": 0, "output": "ok"},
            )
            plan = engine.plan(
                game_id="minecraft",
                control_id="backup-now",
                value=None,
                actor="hermes-desktop",
            )

            with self.assertRaisesRegex(ValueError, "actor does not match"):
                engine.apply(
                    plan_id=plan["planId"],
                    plan_digest=plan["planDigest"],
                    actor="someone-else",
                    confirmed=True,
                )
            with self.assertRaisesRegex(ValueError, "digest does not match"):
                engine.apply(
                    plan_id=plan["planId"],
                    plan_digest="wrong",
                    actor="hermes-desktop",
                    confirmed=True,
                )
            result = engine.apply(
                plan_id=plan["planId"],
                plan_digest=plan["planDigest"],
                actor="hermes-desktop",
                confirmed=True,
            )
            self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
