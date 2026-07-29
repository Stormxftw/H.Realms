import json
import tempfile
import unittest
from pathlib import Path

from control_engine import ControlEngine, ControlEngineError
from registry import ACTION_POLICIES, RISK_LEVELS


class GameRegistryTests(unittest.TestCase):
    @staticmethod
    def _adapter(project_dir="alpha-server", commands=None):
        return {
            "projectDir": project_dir,
            "commands": commands or {},
            "propertyTypes": {},
            "statusCollector": "process_only",
            "processSearch": "alpha-server",
            "defaultPort": 12345,
            "portProtocol": "udp",
        }

    @staticmethod
    def _profile(**overrides):
        profile = {
            "schemaVersion": "1.0",
            "id": "alpha",
            "name": "Alpha",
            "controls": [],
        }
        profile.update(overrides)
        return profile

    def test_backend_action_policy_matches_checked_in_profile_schema(self):
        schema_path = Path(__file__).parents[1] / "schemas" / "game-control-profile.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        self.assertEqual(
            {
                "ui.refresh": ("read-only", False),
                "property.set": ("configuration", True),
                "service.start": ("service", True),
                "service.stop": ("disruptive", True),
                "service.restart": ("disruptive", True),
                "backup.create": ("safe-mutation", True),
            },
            ACTION_POLICIES,
        )
        self.assertEqual(
            set(ACTION_POLICIES),
            set(schema["$defs"]["binding"]["properties"]["action"]["enum"]),
        )
        self.assertEqual(
            list(RISK_LEVELS),
            schema["$defs"]["control"]["properties"]["risk"]["enum"],
        )

    def test_stronger_refresh_warning_does_not_override_backend_confirmation_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            profiles = root / "profiles"
            (projects / "alpha-server").mkdir(parents=True)
            profiles.mkdir()
            adapter_path = root / "game_adapters.json"
            adapter_path.write_text(
                json.dumps({"games": {"alpha": self._adapter()}}),
                encoding="utf-8",
            )
            (profiles / "alpha.json").write_text(
                json.dumps(
                    self._profile(
                        controls=[
                            {
                                "id": "refresh",
                                "kind": "button",
                                "label": "Refresh",
                                "risk": "safe",
                                "binding": {"action": "ui.refresh"},
                            }
                        ]
                    )
                ),
                encoding="utf-8",
            )
            engine = ControlEngine(
                projects_root=projects,
                profiles_dir=profiles,
                audit_path=root / "audit.jsonl",
                adapter_config_path=adapter_path,
            )

            plan = engine.plan(
                game_id="alpha",
                control_id="refresh",
                value=None,
                actor="test",
            )

            self.assertEqual("safe", plan["risk"])
            self.assertFalse(plan["requiresConfirmation"])

    def test_startup_reports_invalid_adapter_json_with_file_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            profiles = root / "profiles"
            projects.mkdir()
            profiles.mkdir()
            adapter_path = root / "game_adapters.json"
            adapter_path.write_text('{"games": ', encoding="utf-8")

            with self.assertRaisesRegex(
                ControlEngineError,
                r"game_adapters\.json: invalid JSON at line 1, column 11",
            ):
                ControlEngine(
                    projects_root=projects,
                    profiles_dir=profiles,
                    audit_path=root / "audit.jsonl",
                    adapter_config_path=adapter_path,
                )

    def test_startup_validates_every_profile_against_checked_in_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            profiles = root / "profiles"
            (projects / "alpha-server").mkdir(parents=True)
            profiles.mkdir()
            adapter_path = root / "game_adapters.json"
            adapter_path.write_text(
                json.dumps({"games": {"alpha": self._adapter()}}),
                encoding="utf-8",
            )
            profile_path = profiles / "alpha.json"
            profile_path.write_text(
                json.dumps(self._profile(unknownField=True)),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ControlEngineError,
                r"alpha\.json: \$: Additional properties are not allowed.*unknownField",
            ):
                ControlEngine(
                    projects_root=projects,
                    profiles_dir=profiles,
                    audit_path=root / "audit.jsonl",
                    adapter_config_path=adapter_path,
                )

    def test_startup_rejects_missing_adapter_profile_pairs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            profiles = root / "profiles"
            (projects / "alpha-server").mkdir(parents=True)
            profiles.mkdir()
            adapter_path = root / "game_adapters.json"
            adapter_path.write_text(
                json.dumps({"games": {"alpha": self._adapter()}}),
                encoding="utf-8",
            )
            (profiles / "beta.json").write_text(
                json.dumps(self._profile(id="beta", name="Beta")),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ControlEngineError,
                r"registry: missing adapter for profile 'beta'; missing profile for adapter 'alpha'",
            ):
                ControlEngine(
                    projects_root=projects,
                    profiles_dir=profiles,
                    audit_path=root / "audit.jsonl",
                    adapter_config_path=adapter_path,
                )

    def test_startup_rejects_duplicate_game_ids_in_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            profiles = root / "profiles"
            (projects / "alpha-server").mkdir(parents=True)
            profiles.mkdir()
            adapter_path = root / "game_adapters.json"
            adapter = json.dumps(self._adapter())
            adapter_path.write_text(
                f'{{"games": {{"alpha": {adapter}, "alpha": {adapter}}}}}',
                encoding="utf-8",
            )
            (profiles / "alpha.json").write_text(
                json.dumps(self._profile()),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ControlEngineError,
                r"game_adapters\.json: \$\.games\.alpha: duplicate field 'alpha'",
            ):
                ControlEngine(
                    projects_root=projects,
                    profiles_dir=profiles,
                    audit_path=root / "audit.jsonl",
                    adapter_config_path=adapter_path,
                )

    def test_startup_rejects_duplicate_control_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            profiles = root / "profiles"
            (projects / "alpha-server").mkdir(parents=True)
            profiles.mkdir()
            adapter_path = root / "game_adapters.json"
            adapter_path.write_text(
                json.dumps({"games": {"alpha": self._adapter()}}),
                encoding="utf-8",
            )
            refresh = {
                "id": "refresh",
                "kind": "button",
                "label": "Refresh",
                "risk": "read-only",
                "binding": {"action": "ui.refresh"},
            }
            (profiles / "alpha.json").write_text(
                json.dumps(self._profile(controls=[refresh, refresh])),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ControlEngineError,
                r"alpha\.json: \$\.controls\[1\]\.id: duplicate control id 'refresh'",
            ):
                ControlEngine(
                    projects_root=projects,
                    profiles_dir=profiles,
                    audit_path=root / "audit.jsonl",
                    adapter_config_path=adapter_path,
                )

    def test_startup_rejects_profile_id_that_does_not_match_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            profiles = root / "profiles"
            (projects / "alpha-server").mkdir(parents=True)
            profiles.mkdir()
            adapter_path = root / "game_adapters.json"
            adapter_path.write_text(
                json.dumps({"games": {"alpha": self._adapter()}}),
                encoding="utf-8",
            )
            (profiles / "alpha.json").write_text(
                json.dumps(self._profile(id="beta")),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ControlEngineError,
                r"alpha\.json: \$\.id: profile id 'beta' must match filename id 'alpha'",
            ):
                ControlEngine(
                    projects_root=projects,
                    profiles_dir=profiles,
                    audit_path=root / "audit.jsonl",
                    adapter_config_path=adapter_path,
                )

    def test_project_directory_rejects_traversal_and_absolute_paths(self):
        for unsafe_path in ("../outside", "/tmp/outside"):
            with self.subTest(projectDir=unsafe_path), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                projects = root / "projects"
                profiles = root / "profiles"
                projects.mkdir()
                profiles.mkdir()
                adapter_path = root / "game_adapters.json"
                adapter_path.write_text(
                    json.dumps(
                        {"games": {"alpha": self._adapter(project_dir=unsafe_path)}}
                    ),
                    encoding="utf-8",
                )
                (profiles / "alpha.json").write_text(
                    json.dumps(self._profile()),
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(
                    ControlEngineError,
                    r"game_adapters\.json: \$\.games\.alpha\.projectDir: must resolve beneath PROJECTS_ROOT",
                ):
                    ControlEngine(
                        projects_root=projects,
                        profiles_dir=profiles,
                        audit_path=root / "audit.jsonl",
                        adapter_config_path=adapter_path,
                    )

    def test_script_rejects_traversal_and_absolute_paths(self):
        for unsafe_path in ("../outside.sh", "/tmp/outside.sh"):
            with self.subTest(script=unsafe_path), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                projects = root / "projects"
                profiles = root / "profiles"
                (projects / "alpha-server").mkdir(parents=True)
                profiles.mkdir()
                adapter_path = root / "game_adapters.json"
                adapter_path.write_text(
                    json.dumps(
                        {
                            "games": {
                                "alpha": self._adapter(
                                    commands={"service.start": [[unsafe_path, 30]]}
                                )
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                (profiles / "alpha.json").write_text(
                    json.dumps(
                        self._profile(
                            controls=[
                                {
                                    "id": "start",
                                    "kind": "button",
                                    "label": "Start",
                                    "risk": "service",
                                    "binding": {"action": "service.start"},
                                }
                            ]
                        )
                    ),
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(
                    ControlEngineError,
                    r"game_adapters\.json: \$\.games\.alpha\.commands\.service\.start\[0\]\[0\]: must resolve beneath game directory",
                ):
                    ControlEngine(
                        projects_root=projects,
                        profiles_dir=profiles,
                        audit_path=root / "audit.jsonl",
                        adapter_config_path=adapter_path,
                    )

    def test_required_scripts_reject_missing_non_executable_and_symlink_files(self):
        for state in ("missing", "non-executable", "symlink"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                projects = root / "projects"
                profiles = root / "profiles"
                game_dir = projects / "alpha-server"
                game_dir.mkdir(parents=True)
                profiles.mkdir()
                script = game_dir / "start.sh"
                if state == "non-executable":
                    script.write_text("#!/bin/sh\n", encoding="utf-8")
                    script.chmod(0o644)
                elif state == "symlink":
                    target = game_dir / "real-start.sh"
                    target.write_text("#!/bin/sh\n", encoding="utf-8")
                    target.chmod(0o755)
                    script.symlink_to(target.name)

                adapter_path = root / "game_adapters.json"
                adapter_path.write_text(
                    json.dumps(
                        {
                            "games": {
                                "alpha": self._adapter(
                                    commands={"service.start": [["start.sh", 30]]}
                                )
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                (profiles / "alpha.json").write_text(
                    json.dumps(
                        self._profile(
                            controls=[
                                {
                                    "id": "start",
                                    "kind": "button",
                                    "label": "Start",
                                    "risk": "service",
                                    "binding": {"action": "service.start"},
                                }
                            ]
                        )
                    ),
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(
                    ControlEngineError,
                    r"game_adapters\.json: \$\.games\.alpha\.commands\.service\.start\[0\]\[0\]: required action script must be a regular executable non-symlink file",
                ):
                    ControlEngine(
                        projects_root=projects,
                        profiles_dir=profiles,
                        audit_path=root / "audit.jsonl",
                        adapter_config_path=adapter_path,
                    )

    def test_startup_validates_scripts_for_adapter_actions_without_profile_controls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            profiles = root / "profiles"
            (projects / "alpha-server").mkdir(parents=True)
            profiles.mkdir()
            adapter_path = root / "game_adapters.json"
            adapter_path.write_text(
                json.dumps(
                    {
                        "games": {
                            "alpha": self._adapter(
                                commands={"service.start": [["missing.sh", 30]]}
                            )
                        }
                    }
                ),
                encoding="utf-8",
            )
            (profiles / "alpha.json").write_text(
                json.dumps(
                    self._profile(
                        controls=[
                            {
                                "id": "refresh",
                                "kind": "button",
                                "label": "Refresh",
                                "risk": "read-only",
                                "binding": {"action": "ui.refresh"},
                            }
                        ]
                    )
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ControlEngineError,
                r"game_adapters\.json: \$\.games\.alpha\.commands\.service\.start\[0\]\[0\]: required action script must be a regular executable non-symlink file",
            ):
                ControlEngine(
                    projects_root=projects,
                    profiles_dir=profiles,
                    audit_path=root / "audit.jsonl",
                    adapter_config_path=adapter_path,
                )

    def test_apply_rejects_script_swapped_to_symlink_after_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            profiles = root / "profiles"
            game_dir = projects / "alpha-server"
            game_dir.mkdir(parents=True)
            profiles.mkdir()
            script = game_dir / "backup.sh"
            script.write_text("#!/bin/sh\n", encoding="utf-8")
            script.chmod(0o755)
            outside = root / "outside.sh"
            outside.write_text("#!/bin/sh\n", encoding="utf-8")
            outside.chmod(0o755)
            adapter_path = root / "game_adapters.json"
            adapter_path.write_text(
                json.dumps(
                    {
                        "games": {
                            "alpha": self._adapter(
                                commands={"backup.create": [["backup.sh", 30]]}
                            )
                        }
                    }
                ),
                encoding="utf-8",
            )
            (profiles / "alpha.json").write_text(
                json.dumps(
                    self._profile(
                        controls=[
                            {
                                "id": "backup",
                                "kind": "button",
                                "label": "Backup",
                                "risk": "safe-mutation",
                                "binding": {"action": "backup.create"},
                            }
                        ]
                    )
                ),
                encoding="utf-8",
            )
            calls = []

            def fake_runner(*args, **kwargs):
                calls.append((args, kwargs))
                return {"ok": True, "exitCode": 0, "output": "unexpected"}

            engine = ControlEngine(
                projects_root=projects,
                profiles_dir=profiles,
                audit_path=root / "audit.jsonl",
                adapter_config_path=adapter_path,
                command_runner=fake_runner,
            )
            plan = engine.plan(
                game_id="alpha",
                control_id="backup",
                value=None,
                actor="test",
            )
            script.unlink()
            script.symlink_to(outside)

            with self.assertRaisesRegex(
                ControlEngineError,
                r"approved action script is no longer a regular executable non-symlink file: backup\.sh",
            ):
                engine.apply(
                    plan_id=plan["planId"],
                    actor="test",
                    confirmed=True,
                    plan_digest=plan["planDigest"],
                )
            self.assertEqual([], calls)

    def test_profile_controls_may_only_reference_adapter_declared_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            profiles = root / "profiles"
            (projects / "alpha-server").mkdir(parents=True)
            profiles.mkdir()
            adapter_path = root / "game_adapters.json"
            adapter_path.write_text(
                json.dumps({"games": {"alpha": self._adapter()}}),
                encoding="utf-8",
            )
            (profiles / "alpha.json").write_text(
                json.dumps(
                    self._profile(
                        controls=[
                            {
                                "id": "start",
                                "kind": "button",
                                "label": "Start",
                                "risk": "service",
                                "binding": {"action": "service.start"},
                            }
                        ]
                    )
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ControlEngineError,
                r"alpha\.json: \$\.controls\[0\]\.binding\.action: action 'service\.start' is not declared by adapter 'alpha'",
            ):
                ControlEngine(
                    projects_root=projects,
                    profiles_dir=profiles,
                    audit_path=root / "audit.jsonl",
                    adapter_config_path=adapter_path,
                )

    def test_startup_rejects_inverted_numeric_range_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            profiles = root / "profiles"
            (projects / "alpha-server").mkdir(parents=True)
            profiles.mkdir()
            adapter = self._adapter()
            adapter["propertyTypes"] = {"setting": "integer"}
            adapter_path = root / "game_adapters.json"
            adapter_path.write_text(
                json.dumps({"games": {"alpha": adapter}}),
                encoding="utf-8",
            )
            (profiles / "alpha.json").write_text(
                json.dumps(
                    self._profile(
                        controls=[
                            {
                                "id": "setting",
                                "kind": "slider",
                                "label": "Setting",
                                "risk": "configuration",
                                "min": 10,
                                "max": 5,
                                "binding": {
                                    "action": "property.set",
                                    "key": "setting",
                                },
                            }
                        ]
                    )
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ControlEngineError,
                r"alpha\.json: \$\.controls\[0\]\.max: must be greater than or equal to min",
            ):
                ControlEngine(
                    projects_root=projects,
                    profiles_dir=profiles,
                    audit_path=root / "audit.jsonl",
                    adapter_config_path=adapter_path,
                )

    def test_property_binding_requires_key_during_startup_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            profiles = root / "profiles"
            (projects / "alpha-server").mkdir(parents=True)
            profiles.mkdir()
            adapter = self._adapter()
            adapter["propertyTypes"] = {"known-key": "string"}
            adapter_path = root / "game_adapters.json"
            adapter_path.write_text(
                json.dumps({"games": {"alpha": adapter}}),
                encoding="utf-8",
            )
            (profiles / "alpha.json").write_text(
                json.dumps(
                    self._profile(
                        controls=[
                            {
                                "id": "property",
                                "kind": "text",
                                "label": "Property",
                                "risk": "configuration",
                                "binding": {"action": "property.set"},
                            }
                        ]
                    )
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ControlEngineError,
                r"alpha\.json: \$\.controls\[0\]\.binding: 'key' is a required property",
            ):
                ControlEngine(
                    projects_root=projects,
                    profiles_dir=profiles,
                    audit_path=root / "audit.jsonl",
                    adapter_config_path=adapter_path,
                )

    def test_non_property_binding_rejects_key_during_startup_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            profiles = root / "profiles"
            (projects / "alpha-server").mkdir(parents=True)
            profiles.mkdir()
            adapter_path = root / "game_adapters.json"
            adapter_path.write_text(
                json.dumps({"games": {"alpha": self._adapter()}}),
                encoding="utf-8",
            )
            (profiles / "alpha.json").write_text(
                json.dumps(
                    self._profile(
                        controls=[
                            {
                                "id": "refresh",
                                "kind": "button",
                                "label": "Refresh",
                                "risk": "read-only",
                                "binding": {
                                    "action": "ui.refresh",
                                    "key": "unexpected",
                                },
                            }
                        ]
                    )
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ControlEngineError,
                r"alpha\.json: \$\.controls\[0\]\.binding:",
            ):
                ControlEngine(
                    projects_root=projects,
                    profiles_dir=profiles,
                    audit_path=root / "audit.jsonl",
                    adapter_config_path=adapter_path,
                )

    def test_profile_property_binding_key_must_be_declared_by_adapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            profiles = root / "profiles"
            (projects / "alpha-server").mkdir(parents=True)
            profiles.mkdir()
            adapter = self._adapter()
            adapter["propertyTypes"] = {"known-key": "string"}
            adapter_path = root / "game_adapters.json"
            adapter_path.write_text(
                json.dumps({"games": {"alpha": adapter}}),
                encoding="utf-8",
            )
            (profiles / "alpha.json").write_text(
                json.dumps(
                    self._profile(
                        controls=[
                            {
                                "id": "unknown-property",
                                "kind": "text",
                                "label": "Unknown property",
                                "risk": "configuration",
                                "binding": {
                                    "action": "property.set",
                                    "key": "unknown-key",
                                },
                            }
                        ]
                    )
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ControlEngineError,
                r"alpha\.json: \$\.controls\[0\]\.binding\.key: property key 'unknown-key' is not declared by adapter 'alpha'",
            ):
                ControlEngine(
                    projects_root=projects,
                    profiles_dir=profiles,
                    audit_path=root / "audit.jsonl",
                    adapter_config_path=adapter_path,
                )

    def test_profile_cannot_downgrade_backend_action_risk(self):
        cases = (
            ("property.set", "safe-mutation", "configuration"),
            ("service.start", "configuration", "service"),
            ("service.stop", "service", "disruptive"),
            ("service.restart", "service", "disruptive"),
            ("backup.create", "safe", "safe-mutation"),
        )
        for action, claimed_risk, backend_risk in cases:
            with self.subTest(action=action), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                projects = root / "projects"
                profiles = root / "profiles"
                game_dir = projects / "alpha-server"
                game_dir.mkdir(parents=True)
                profiles.mkdir()
                adapter = self._adapter()
                binding = {"action": action}
                if action == "property.set":
                    adapter["propertyTypes"] = {"setting": "string"}
                    binding["key"] = "setting"
                else:
                    script_name = f"{action.replace('.', '-')}.sh"
                    script = game_dir / script_name
                    script.write_text("#!/bin/sh\n", encoding="utf-8")
                    script.chmod(0o755)
                    adapter["commands"] = {action: [[script_name, 30]]}
                adapter_path = root / "game_adapters.json"
                adapter_path.write_text(
                    json.dumps({"games": {"alpha": adapter}}),
                    encoding="utf-8",
                )
                (profiles / "alpha.json").write_text(
                    json.dumps(
                        self._profile(
                            controls=[
                                {
                                    "id": "action",
                                    "kind": "text" if action == "property.set" else "button",
                                    "label": "Action",
                                    "risk": claimed_risk,
                                    "binding": binding,
                                }
                            ]
                        )
                    ),
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(
                    ControlEngineError,
                    rf"alpha\.json: \$\.controls\[0\]\.risk: action '{action}' requires backend risk '{backend_risk}'",
                ):
                    ControlEngine(
                        projects_root=projects,
                        profiles_dir=profiles,
                        audit_path=root / "audit.jsonl",
                        adapter_config_path=adapter_path,
                    )


if __name__ == "__main__":
    unittest.main()
