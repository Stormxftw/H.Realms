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

    @classmethod
    def _property_control_engine(cls, root, *, property_type, kind, action="property.set"):
        projects = root / "projects"
        profiles = root / "profiles"
        (projects / "alpha-server").mkdir(parents=True)
        profiles.mkdir()
        adapter = cls._adapter()
        adapter["propertyTypes"] = (
            {"setting": property_type} if property_type is not None else {}
        )
        adapter_path = root / "game_adapters.json"
        adapter_path.write_text(
            json.dumps({"games": {"alpha": adapter}}), encoding="utf-8"
        )
        binding = {"action": action}
        if action == "property.set":
            binding["key"] = "setting"
        (profiles / "alpha.json").write_text(
            json.dumps(
                cls._profile(
                    controls=[
                        {
                            "id": "setting",
                            "kind": kind,
                            "label": "Setting",
                            "risk": (
                                "configuration"
                                if action == "property.set"
                                else "read-only"
                            ),
                            "binding": binding,
                        }
                    ]
                )
            ),
            encoding="utf-8",
        )
        return ControlEngine(
            projects_root=projects,
            profiles_dir=profiles,
            audit_path=root / "audit.jsonl",
            adapter_config_path=adapter_path,
        )

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

    def test_startup_rejects_non_finite_json_constants_with_file_diagnostics(self):
        for document, constant in (
            ("adapter", "NaN"),
            ("adapter", "Infinity"),
            ("profile", "NaN"),
            ("profile", "Infinity"),
        ):
            with self.subTest(document=document, constant=constant), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                projects = root / "projects"
                profiles = root / "profiles"
                (projects / "alpha-server").mkdir(parents=True)
                profiles.mkdir()
                adapter_path = root / "game_adapters.json"
                profile_path = profiles / "alpha.json"
                if document == "adapter":
                    adapter_path.write_text(
                        json.dumps({"games": {"alpha": self._adapter()}}).replace(
                            '"defaultPort": 12345', f'"defaultPort": {constant}'
                        ),
                        encoding="utf-8",
                    )
                    profile_path.write_text(
                        json.dumps(self._profile()), encoding="utf-8"
                    )
                    offending_path = adapter_path
                else:
                    adapter_path.write_text(
                        json.dumps({"games": {"alpha": self._adapter()}}),
                        encoding="utf-8",
                    )
                    profile_path.write_text(
                        json.dumps(self._profile()).replace(
                            '"name": "Alpha"', f'"name": {constant}'
                        ),
                        encoding="utf-8",
                    )
                    offending_path = profile_path

                with self.assertRaisesRegex(
                    ControlEngineError,
                    rf"{offending_path.name}: invalid JSON: non-finite numeric constant '{constant}'",
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

    def test_project_directory_rejects_root_equality_traversal_and_absolute_paths(self):
        for unsafe_path in (".", "../outside", "/tmp/outside"):
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

    def test_startup_rejects_empty_command_sequence_at_exact_json_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            profiles = root / "profiles"
            projects.mkdir()
            profiles.mkdir()
            adapter_path = root / "game_adapters.json"
            adapter_path.write_text(
                json.dumps(
                    {
                        "games": {
                            "alpha": self._adapter(
                                commands={"service.start": []}
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
                r"game_adapters\.json: \$\.games\.alpha\.commands\.service\.start: .*non-empty",
            ):
                ControlEngine(
                    projects_root=projects,
                    profiles_dir=profiles,
                    audit_path=root / "audit.jsonl",
                    adapter_config_path=adapter_path,
                )

    def test_catalog_only_game_with_absent_project_starts_but_plan_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            profiles = root / "profiles"
            projects.mkdir()
            profiles.mkdir()
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

            engine = ControlEngine(
                projects_root=projects,
                profiles_dir=profiles,
                audit_path=root / "audit.jsonl",
                adapter_config_path=adapter_path,
            )

            self.assertEqual(["alpha"], [game["id"] for game in engine.catalog()["games"]])
            with self.assertRaisesRegex(
                ControlEngineError,
                r"action unavailable for alpha: approved project path is unavailable",
            ):
                engine.plan(
                    game_id="alpha",
                    control_id="start",
                    value=None,
                    actor="test",
                )

    def test_installed_game_with_unavailable_required_script_is_not_registry_fatal(self):
        cases = (
            (
                "missing",
                "start.sh",
                r"approved action script is no longer a regular executable non-symlink file: start\.sh",
            ),
            (
                "non-executable",
                "start.sh",
                r"approved action script is no longer a regular executable non-symlink file: start\.sh",
            ),
            (
                "final-symlink",
                "start.sh",
                r"approved action script is no longer a regular executable non-symlink file: start\.sh",
            ),
            (
                "ancestor-symlink",
                "scripts/start.sh",
                r"approved action script path contains a symlink component",
            ),
        )
        for state, script_name, diagnostic in cases:
            with self.subTest(state=state), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                projects = root / "projects"
                profiles = root / "profiles"
                game_dir = projects / "alpha-server"
                game_dir.mkdir(parents=True)
                profiles.mkdir()
                script = game_dir / script_name
                if state == "non-executable":
                    script.write_text("#!/bin/sh\n", encoding="utf-8")
                    script.chmod(0o644)
                elif state == "final-symlink":
                    target = game_dir / "real-start.sh"
                    target.write_text("#!/bin/sh\n", encoding="utf-8")
                    target.chmod(0o755)
                    script.symlink_to(target.name)
                elif state == "ancestor-symlink":
                    real_scripts = game_dir / "real-scripts"
                    real_scripts.mkdir()
                    target = real_scripts / "start.sh"
                    target.write_text("#!/bin/sh\n", encoding="utf-8")
                    target.chmod(0o755)
                    script.parent.symlink_to(real_scripts.name, target_is_directory=True)
                adapter_path = root / "game_adapters.json"
                adapter_path.write_text(
                    json.dumps(
                        {
                            "games": {
                                "alpha": self._adapter(
                                    commands={"service.start": [[script_name, 30]]}
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

                engine = ControlEngine(
                    projects_root=projects,
                    profiles_dir=profiles,
                    audit_path=root / "audit.jsonl",
                    adapter_config_path=adapter_path,
                )

                self.assertEqual("alpha", engine.catalog()["games"][0]["id"])
                with self.assertRaisesRegex(
                    ControlEngineError,
                    rf"action unavailable for alpha: {diagnostic}",
                ):
                    engine.plan(
                        game_id="alpha",
                        control_id="start",
                        value=None,
                        actor="test",
                    )

    def test_absent_catalog_project_does_not_skip_script_lexical_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            profiles = root / "profiles"
            projects.mkdir()
            profiles.mkdir()
            adapter_path = root / "game_adapters.json"
            adapter_path.write_text(
                json.dumps(
                    {
                        "games": {
                            "alpha": self._adapter(
                                commands={"service.start": [["../outside.sh", 30]]}
                            )
                        }
                    }
                ),
                encoding="utf-8",
            )
            (profiles / "alpha.json").write_text(
                json.dumps(self._profile()), encoding="utf-8"
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

    def test_property_control_kind_matches_adapter_property_type_matrix(self):
        allowed = {
            "integer": {"slider", "number"},
            "boolean": {"switch"},
            "string": {"text", "select"},
        }
        mutating_kinds = {"slider", "number", "switch", "text", "select", "readonly"}
        for property_type, allowed_kinds in allowed.items():
            for kind in sorted(mutating_kinds):
                with self.subTest(property_type=property_type, kind=kind), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    if kind in allowed_kinds:
                        engine = self._property_control_engine(
                            root, property_type=property_type, kind=kind
                        )
                        self.assertEqual(("alpha",), engine._registry.game_ids)
                    else:
                        with self.assertRaisesRegex(
                            ControlEngineError,
                            rf"alpha\.json: \$\.controls\[0\]\.kind: control kind '{kind}' is incompatible with property type '{property_type}' for binding key 'setting'",
                        ):
                            self._property_control_engine(
                                root, property_type=property_type, kind=kind
                            )

    def test_readonly_control_remains_valid_with_nonmutating_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._property_control_engine(
                Path(tmp),
                property_type=None,
                kind="readonly",
                action="ui.refresh",
            )

            self.assertEqual("readonly", engine.catalog()["games"][0]["controls"][0]["kind"])

    def test_property_plan_rejects_missing_properties_file_as_unavailable(self):
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
                json.dumps({"games": {"alpha": adapter}}), encoding="utf-8"
            )
            (profiles / "alpha.json").write_text(
                json.dumps(
                    self._profile(
                        controls=[
                            {
                                "id": "setting",
                                "kind": "number",
                                "label": "Setting",
                                "risk": "configuration",
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
            engine = ControlEngine(
                projects_root=projects,
                profiles_dir=profiles,
                audit_path=root / "audit.jsonl",
                adapter_config_path=adapter_path,
            )

            with self.assertRaisesRegex(
                ControlEngineError,
                r"action unavailable for alpha: approved property path is unavailable",
            ):
                engine.plan(
                    game_id="alpha",
                    control_id="setting",
                    value=2,
                    actor="test",
                )

    def test_apply_revalidates_unavailable_script_states_before_runner(self):
        cases = (
            (
                "missing",
                r"approved action script is no longer a regular executable non-symlink file: backup\.sh",
            ),
            (
                "non-executable",
                r"approved action script is no longer a regular executable non-symlink file: backup\.sh",
            ),
            (
                "final-symlink",
                r"approved action script is no longer a regular executable non-symlink file: backup\.sh",
            ),
            (
                "ancestor-symlink",
                r"approved action script path contains a symlink component",
            ),
        )
        for state, diagnostic in cases:
            with self.subTest(state=state), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                projects = root / "projects"
                profiles = root / "profiles"
                game_dir = projects / "alpha-server"
                scripts_dir = game_dir / "scripts"
                scripts_dir.mkdir(parents=True)
                profiles.mkdir()
                script = scripts_dir / "backup.sh"
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
                                    commands={
                                        "backup.create": [["scripts/backup.sh", 30]]
                                    }
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
                if state == "missing":
                    script.unlink()
                elif state == "non-executable":
                    script.chmod(0o644)
                elif state == "final-symlink":
                    script.unlink()
                    script.symlink_to(outside)
                else:
                    original_scripts = game_dir / "scripts-original"
                    scripts_dir.rename(original_scripts)
                    replacement_scripts = game_dir / "replacement-scripts"
                    replacement_scripts.mkdir()
                    replacement = replacement_scripts / "backup.sh"
                    replacement.write_text("#!/bin/sh\n", encoding="utf-8")
                    replacement.chmod(0o755)
                    scripts_dir.symlink_to(
                        replacement_scripts.name, target_is_directory=True
                    )

                with self.assertRaisesRegex(ControlEngineError, diagnostic):
                    engine.apply(
                        plan_id=plan["planId"],
                        actor="test",
                        confirmed=True,
                        plan_digest=plan["planDigest"],
                    )
                self.assertEqual([], calls)

    def test_apply_rejects_project_directory_swapped_to_outside_symlink(self):
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
            outside = root / "outside-game"
            outside.mkdir()
            outside_script = outside / "backup.sh"
            outside_script.write_text("#!/bin/sh\n", encoding="utf-8")
            outside_script.chmod(0o755)
            sentinel = outside / "sentinel.bin"
            sentinel.write_bytes(b"outside-must-not-change")
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
            engine = ControlEngine(
                projects_root=projects,
                profiles_dir=profiles,
                audit_path=root / "audit.jsonl",
                adapter_config_path=adapter_path,
                command_runner=lambda *args, **kwargs: (
                    calls.append((args, kwargs))
                    or {"ok": True, "exitCode": 0, "output": "unexpected"}
                ),
            )
            plan = engine.plan(
                game_id="alpha", control_id="backup", value=None, actor="test"
            )
            game_dir.rename(projects / "alpha-server-original")
            game_dir.symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(
                ControlEngineError,
                r"approved project path contains a symlink component",
            ):
                engine.apply(
                    plan_id=plan["planId"],
                    actor="test",
                    confirmed=True,
                    plan_digest=plan["planDigest"],
                )
            self.assertEqual([], calls)
            self.assertEqual(b"outside-must-not-change", sentinel.read_bytes())

    def test_apply_rejects_parent_swapped_to_outside_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            container = root / "container"
            projects = container / "projects"
            profiles = root / "profiles"
            game_dir = projects / "alpha-server"
            game_dir.mkdir(parents=True)
            profiles.mkdir()
            script = game_dir / "backup.sh"
            script.write_text("#!/bin/sh\n", encoding="utf-8")
            script.chmod(0o755)
            outside_container = root / "outside-container"
            outside_game = outside_container / "projects" / "alpha-server"
            outside_game.mkdir(parents=True)
            outside_script = outside_game / "backup.sh"
            outside_script.write_text("#!/bin/sh\n", encoding="utf-8")
            outside_script.chmod(0o755)
            sentinel = outside_game / "sentinel.bin"
            sentinel.write_bytes(b"outside-parent-must-not-change")
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
            engine = ControlEngine(
                projects_root=projects,
                profiles_dir=profiles,
                audit_path=root / "audit.jsonl",
                adapter_config_path=adapter_path,
                command_runner=lambda *args, **kwargs: (
                    calls.append((args, kwargs))
                    or {"ok": True, "exitCode": 0, "output": "unexpected"}
                ),
            )
            plan = engine.plan(
                game_id="alpha", control_id="backup", value=None, actor="test"
            )
            container.rename(root / "container-original")
            container.symlink_to(outside_container, target_is_directory=True)

            with self.assertRaisesRegex(
                ControlEngineError,
                r"approved project path contains a symlink component",
            ):
                engine.apply(
                    plan_id=plan["planId"],
                    actor="test",
                    confirmed=True,
                    plan_digest=plan["planDigest"],
                )
            self.assertEqual([], calls)
            self.assertEqual(b"outside-parent-must-not-change", sentinel.read_bytes())

    def test_property_apply_uses_validated_project_path_after_symlink_swap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            profiles = root / "profiles"
            game_dir = projects / "alpha-server"
            game_dir.mkdir(parents=True)
            profiles.mkdir()
            properties = game_dir / "server.properties"
            properties.write_text("setting=1\n", encoding="utf-8")
            outside = root / "outside-game"
            outside.mkdir()
            outside_properties = outside / "server.properties"
            outside_properties.write_bytes(b"setting=9\n")
            adapter = self._adapter()
            adapter["propertyTypes"] = {"setting": "integer"}
            adapter_path = root / "game_adapters.json"
            adapter_path.write_text(
                json.dumps({"games": {"alpha": adapter}}), encoding="utf-8"
            )
            (profiles / "alpha.json").write_text(
                json.dumps(
                    self._profile(
                        controls=[
                            {
                                "id": "setting",
                                "kind": "number",
                                "label": "Setting",
                                "risk": "configuration",
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
            engine = ControlEngine(
                projects_root=projects,
                profiles_dir=profiles,
                audit_path=root / "audit.jsonl",
                adapter_config_path=adapter_path,
            )
            plan = engine.plan(
                game_id="alpha", control_id="setting", value=2, actor="test"
            )
            game_dir.rename(projects / "alpha-server-original")
            game_dir.symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(
                ControlEngineError,
                r"approved project path contains a symlink component",
            ):
                engine.apply(
                    plan_id=plan["planId"],
                    actor="test",
                    confirmed=True,
                    plan_digest=plan["planDigest"],
                )
            self.assertEqual(b"setting=9\n", outside_properties.read_bytes())

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
