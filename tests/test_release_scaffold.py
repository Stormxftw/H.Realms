from __future__ import annotations

import os
import json
import re
import shutil
import stat
import subprocess
import tempfile
import tomllib
import unittest
from contextlib import contextmanager
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RELEASE_CHECK = ROOT / "scripts" / "release-check.sh"

# One or more committed sentinels from every artifact category needed to
# reproduce the v1 console from a release archive.
REQUIRED_RELEASE_ARTIFACTS = (
    "pyproject.toml",
    "app.py",
    "control_engine.py",
    "telemetry.py",
    "registry.py",
    "operations.py",
    "restart_state.py",
    "store.py",
    "data/schema.sql",
    "game_adapters.json",
    "static/index.html",
    "static/app.css",
    "static/app.js",
    "desktop-plugin/plugin.js",
    "hermes-plugin/plugin.yaml",
    "hermes-plugin/dashboard/manifest.json",
    "hermes-plugin/dashboard/plugin_api.py",
    "hermes-plugin/dashboard/dist/index.js",
    "schemas/game-adapter-config.schema.json",
    "schemas/game-control-profile.schema.json",
    "game_profiles/_template.json",
    "game_profiles/cs2.json",
    "game_profiles/dont-starve-together.json",
    "game_profiles/enshrouded.json",
    "game_profiles/minecraft.json",
    "game_profiles/palworld.json",
    "game_profiles/satisfactory.json",
    "game_profiles/sons-of-the-forest.json",
    "game_profiles/terraria.json",
    "game_profiles/valheim.json",
    "tests/test_app_api.py",
    "tests/test_control_engine.py",
    "tests/test_registry.py",
    "tests/test_plugin_api.py",
    "tests/test_readiness.py",
    "tests/test_telemetry.py",
    "tests/test_operations.py",
    "tests/test_restart_state.py",
    "tests/test_store_api.py",
    "tests/ui.test.js",
    "tests/desktop_plugin.test.js",
    "tests/tsconfig.desktop-plugin.json",
    "tests/test_release_scaffold.py",
    "scripts/release-check.sh",
    "start.sh",
    "status.sh",
    "stop.sh",
    "install-hermes-plugin.sh",
    "uninstall-hermes-plugin.sh",
)

REQUIRED_EXECUTABLE_ARTIFACTS = (
    "scripts/release-check.sh",
    "start.sh",
    "status.sh",
    "stop.sh",
    "install-hermes-plugin.sh",
    "uninstall-hermes-plugin.sh",
)

CATEGORY_SENTINELS = {
    "dependency manifest": "pyproject.toml",
    "Python source": "app.py",
    "adapter registry": "game_adapters.json",
    "standalone UI": "static/app.js",
    "Desktop plugin": "desktop-plugin/plugin.js",
    "Hermes bridge": "hermes-plugin/plugin.yaml",
    "schemas": "schemas/game-control-profile.schema.json",
    "profiles/template": "game_profiles/_template.json",
    "profiles/cs2": "game_profiles/cs2.json",
    "profiles/dont-starve-together": "game_profiles/dont-starve-together.json",
    "profiles/enshrouded": "game_profiles/enshrouded.json",
    "profiles/minecraft": "game_profiles/minecraft.json",
    "profiles/palworld": "game_profiles/palworld.json",
    "profiles/satisfactory": "game_profiles/satisfactory.json",
    "profiles/sons-of-the-forest": "game_profiles/sons-of-the-forest.json",
    "profiles/terraria": "game_profiles/terraria.json",
    "profiles/valheim": "game_profiles/valheim.json",
    "tests": "tests/ui.test.js",
    "lifecycle scripts": "start.sh",
}


class ReleaseScaffoldTests(unittest.TestCase):
    def test_pyproject_declares_supported_python_and_only_actual_dependencies(self):
        manifest_path = ROOT / "pyproject.toml"
        self.assertTrue(manifest_path.is_file(), "pyproject.toml must exist")
        manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
        project = manifest["project"]

        self.assertEqual(">=3.11", project["requires-python"])

        def dependency_names(dependencies: list[str]) -> set[str]:
            names = set()
            for dependency in dependencies:
                match = re.match(r"[A-Za-z0-9_.-]+", dependency)
                if match is None:
                    self.fail(f"invalid dependency declaration: {dependency}")
                names.add(match.group(0).lower())
            return names

        self.assertEqual(
            {"fastapi", "jsonschema"},
            dependency_names(project["dependencies"]),
        )
        self.assertEqual(
            set(),
            dependency_names(project["optional-dependencies"]["test"]),
        )

    def test_desktop_plugin_compiler_rejects_jsdoc_type_violations(self):
        tsc = Path(
            os.environ.get(
                "TSC_BIN",
                Path.home() / ".hermes" / "hermes-agent" / "node_modules" / ".bin" / "tsc",
            )
        )
        self.assertTrue(tsc.is_file(), f"TypeScript compiler not found: {tsc}")

        with tempfile.TemporaryDirectory() as tmp:
            probe_root = Path(tmp)
            probe = probe_root / "type-violation.js"
            probe.write_text(
                "/** @type {number} */\nconst releaseTypeProbe = 'not a number'\n"
                "void releaseTypeProbe\n",
                encoding="utf-8",
            )
            config = probe_root / "tsconfig.json"
            config.write_text(
                json.dumps(
                    {
                        "extends": str(ROOT / "tests" / "tsconfig.desktop-plugin.json"),
                        "files": [str(probe)],
                    }
                ),
                encoding="utf-8",
            )

            result = subprocess.run(
                [str(tsc), "--project", str(config), "--noEmit", "--pretty", "false"],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(0, result.returncode, "checkJs must reject explicit JSDoc violations")
            self.assertIn("not assignable to type 'number'", result.stdout + result.stderr)

    def test_gitignore_covers_generated_artifacts_without_hiding_sources(self):
        generated = (
            "logs/release-check.log",
            "dashboard.pid",
            "__pycache__/app.cpython-311.pyc",
            ".pytest_cache/state",
            ".install-backups/previous/plugin.yaml",
            ".uninstall-backups/previous/plugin.yaml",
            "data/operations.sqlite3",
            "data/operations.sqlite3-wal",
            "data/operations.sqlite3-shm",
            "data/operations.sqlite3-journal",
            "data/operation-state.json",
            "data/control-audit.jsonl",
            "tests/browser/output/results.json",
            "playwright-report/index.html",
            "downloads/game-server.tar.gz",
        )
        sources = (
            "data/schema.sql",
            "requirements.in",
            "scripts/new-release-check.sh",
            "tests/browser/test_release.py",
            "game_profiles/new-game.json",
        )

        for relative in generated:
            with self.subTest(generated=relative):
                result = subprocess.run(
                    ["git", "check-ignore", "--no-index", "-q", "--", relative],
                    cwd=ROOT,
                    check=False,
                )
                self.assertEqual(0, result.returncode, f"generated artifact not ignored: {relative}")
        for relative in sources:
            with self.subTest(source=relative):
                result = subprocess.run(
                    ["git", "check-ignore", "--no-index", "-q", "--", relative],
                    cwd=ROOT,
                    check=False,
                )
                self.assertNotEqual(0, result.returncode, f"source path is hidden: {relative}")

    @contextmanager
    def release_fixture(self, *, omit_artifact: str | None = None):
        self.assertTrue(RELEASE_CHECK.is_file(), "scripts/release-check.sh must exist")
        real_git = shutil.which("git")
        real_tar = shutil.which("tar")
        self.assertIsNotNone(real_git)
        self.assertIsNotNone(real_tar)

        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            root = temp_root / "repo"
            root.mkdir()
            for relative in REQUIRED_RELEASE_ARTIFACTS:
                if relative == omit_artifact:
                    continue
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if relative == "scripts/release-check.sh":
                    target.write_bytes(RELEASE_CHECK.read_bytes())
                else:
                    target.write_text(f"fixture for {relative}\n", encoding="utf-8")
                if relative in REQUIRED_EXECUTABLE_ARTIFACTS:
                    target.chmod(target.stat().st_mode | stat.S_IXUSR)

            subprocess.run([real_git, "init", "-q"], cwd=root, check=True)
            tracked = [path for path in REQUIRED_RELEASE_ARTIFACTS if path != omit_artifact]
            subprocess.run([real_git, "add", "--", *tracked], cwd=root, check=True)
            subprocess.run(
                [
                    real_git,
                    "-c",
                    "user.name=Release Scaffold Test",
                    "-c",
                    "user.email=release-scaffold@example.invalid",
                    "commit",
                    "-qm",
                    "fixture",
                ],
                cwd=root,
                check=True,
            )

            fake_log = temp_root / "fake-tools.log"
            tool_dir = temp_root / "fake-tools"
            tool_dir.mkdir()
            shim = """#!/usr/bin/env bash
set -eu
tool="$(basename "$0")"
printf '%s:%s\\n' "$tool" "$*" >> "$FAKE_LOG"
if [[ -n "${FAKE_MUTATE_MATCH:-}" && "$tool:$*" == *"$FAKE_MUTATE_MATCH"* ]]; then
  printf '\\nchanged during checks\\n' >> "$FAKE_MUTATE_PATH"
fi
if [[ -n "${FAKE_FAIL_MATCH:-}" && "$tool:$*" == *"$FAKE_FAIL_MATCH"* ]]; then
  exit 19
fi
if [[ "$tool" == git ]]; then
  exec "$REAL_GIT" "$@"
fi
exit 0
"""
            for tool in ("python", "node", "hermes-python", "tsc", "git"):
                path = tool_dir / tool
                path.write_text(shim, encoding="utf-8")
                path.chmod(path.stat().st_mode | stat.S_IXUSR)

            env = os.environ.copy()
            env.update(
                {
                    "PYTHON_BIN": str(tool_dir / "python"),
                    "NODE_BIN": str(tool_dir / "node"),
                    "HERMES_PYTHON": str(tool_dir / "hermes-python"),
                    "TSC_BIN": str(tool_dir / "tsc"),
                    "GIT_BIN": str(tool_dir / "git"),
                    "TAR_BIN": real_tar,
                    "FAKE_LOG": str(fake_log),
                    "REAL_GIT": real_git,
                }
            )
            yield root, env, fake_log

    def test_release_check_propagates_failure_and_stops_later_steps(self):
        with self.release_fixture() as (root, env, fake_log):
            env["FAKE_FAIL_MATCH"] = "node:tests/ui.test.js"
            result = subprocess.run(
                [str(root / "scripts" / "release-check.sh")],
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(0, result.returncode)
            calls = fake_log.read_text(encoding="utf-8")
            self.assertIn("python:-m unittest discover -s tests -v", calls)
            self.assertIn("node:tests/ui.test.js", calls)
            self.assertNotIn("node:tests/desktop_plugin.test.js", calls)
            self.assertNotIn("hermes-python:", calls)

    def assert_dirty_source_rejected(
        self,
        *,
        relative: str,
        content: str,
        stage: bool,
    ) -> None:
        with self.release_fixture() as (root, env, fake_log):
            source_path = root / relative
            source_path.write_text(content, encoding="utf-8")
            if stage:
                subprocess.run([env["REAL_GIT"], "add", "--", relative], cwd=root, check=True)

            result = subprocess.run(
                [str(root / "scripts" / "release-check.sh")],
                cwd=root.parent,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertIn("release source tree must be clean", result.stderr)
            self.assertIn(relative, result.stderr)
            calls = fake_log.read_text(encoding="utf-8")
            self.assertIn("git:status --porcelain=v1 --untracked-files=all --ignored=no", calls)
            self.assertNotIn("python:", calls, "checks must not run against mixed source bytes")

    def test_release_check_rejects_staged_source_bytes_before_checks(self):
        self.assert_dirty_source_rejected(
            relative="app.py",
            content="valid staged bytes\n",
            stage=True,
        )

    def test_release_check_rejects_unstaged_source_bytes_before_checks(self):
        self.assert_dirty_source_rejected(
            relative="app.py",
            content="different unstaged bytes\n",
            stage=False,
        )

    def test_release_check_rejects_untracked_source_before_checks(self):
        self.assert_dirty_source_rejected(
            relative="new-source.py",
            content="untracked source bytes\n",
            stage=False,
        )

    def test_release_check_cleans_archive_tempdir_when_archive_creation_fails(self):
        with self.release_fixture() as (root, env, _):
            release_tmp = root.parent / "release-tmp"
            release_tmp.mkdir()
            env["TMPDIR"] = str(release_tmp)
            env["FAKE_FAIL_MATCH"] = "git:archive --format=tar"

            result = subprocess.run(
                [str(root / "scripts" / "release-check.sh")],
                cwd=root.parent,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertEqual([], list(release_tmp.glob("game-host-release-check.*")))

    def test_release_check_rejects_source_changed_during_checks_before_archiving(self):
        with self.release_fixture() as (root, env, fake_log):
            env["FAKE_MUTATE_MATCH"] = "python:-m unittest discover"
            env["FAKE_MUTATE_PATH"] = str(root / "app.py")

            result = subprocess.run(
                [str(root / "scripts" / "release-check.sh")],
                cwd=root.parent,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertIn("release source tree must be clean", result.stderr)
            calls = fake_log.read_text(encoding="utf-8")
            self.assertNotIn("git:archive", calls)

    def test_release_check_runs_canonical_checks_in_order_from_another_cwd(self):
        with self.release_fixture() as (root, env, fake_log):
            result = subprocess.run(
                [str(root / "scripts" / "release-check.sh")],
                cwd=root.parent,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            calls = fake_log.read_text(encoding="utf-8").splitlines()
            expected_calls = (
                "python:-m unittest discover -s tests -v",
                "node:tests/ui.test.js",
                "node:tests/desktop_plugin.test.js",
                "hermes-python:-m unittest tests.test_plugin_api -v",
                "python:- ",
                "tsc:--project tests/tsconfig.desktop-plugin.json --noEmit --pretty false",
                "git:diff --check",
                "git:archive --format=tar --output=",
            )
            cursor = 0
            for expected in expected_calls:
                while cursor < len(calls) and expected not in calls[cursor]:
                    cursor += 1
                self.assertLess(cursor, len(calls), f"missing canonical call: {expected}")
                cursor += 1

    def test_release_check_discovers_hermes_tools_from_home(self):
        with self.release_fixture() as (root, env, fake_log):
            fake_home = root.parent / "portable-home"
            home_python = fake_home / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"
            home_tsc = fake_home / ".hermes" / "hermes-agent" / "node_modules" / ".bin" / "tsc"
            for tool, label in (
                (home_python, "home-hermes-python"),
                (home_tsc, "home-tsc"),
            ):
                tool.parent.mkdir(parents=True, exist_ok=True)
                tool.write_text(
                    "#!/usr/bin/env bash\n"
                    f"printf '{label}:%s\\n' \"$*\" >> \"$FAKE_LOG\"\n",
                    encoding="utf-8",
                )
                tool.chmod(tool.stat().st_mode | stat.S_IXUSR)

            env["HOME"] = str(fake_home)
            del env["HERMES_PYTHON"]
            del env["TSC_BIN"]

            result = subprocess.run(
                [str(root / "scripts" / "release-check.sh")],
                cwd=root.parent,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            calls = fake_log.read_text(encoding="utf-8")
            self.assertIn("home-hermes-python:-m unittest tests.test_plugin_api -v", calls)
            self.assertIn(
                "home-tsc:--project tests/tsconfig.desktop-plugin.json --noEmit --pretty false",
                calls,
            )
            self.assertNotIn("/home/user", RELEASE_CHECK.read_text(encoding="utf-8"))

    def test_release_archive_requires_every_v1_artifact_category(self):
        for category, sentinel in CATEGORY_SENTINELS.items():
            with self.subTest(category=category, sentinel=sentinel):
                with self.release_fixture(omit_artifact=sentinel) as (root, env, _):
                    result = subprocess.run(
                        [str(root / "scripts" / "release-check.sh")],
                        cwd=root,
                        env=env,
                        text=True,
                        capture_output=True,
                        check=False,
                    )

                    self.assertNotEqual(0, result.returncode)
                    self.assertIn(
                        f"missing required tracked release artifact: {sentinel}",
                        result.stderr,
                    )

    def test_release_archive_requires_lifecycle_scripts_to_be_executable(self):
        for relative in REQUIRED_EXECUTABLE_ARTIFACTS:
            with self.subTest(relative=relative):
                with self.release_fixture() as (root, env, _):
                    subprocess.run(
                        [env["REAL_GIT"], "update-index", "--chmod=-x", "--", relative],
                        cwd=root,
                        check=True,
                    )
                    script_path = root / relative
                    script_path.chmod(
                        script_path.stat().st_mode
                        & ~(stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                    )
                    subprocess.run(
                        [
                            env["REAL_GIT"],
                            "-c",
                            "user.name=Release Scaffold Test",
                            "-c",
                            "user.email=release-scaffold@example.invalid",
                            "commit",
                            "-qm",
                            "remove executable bit",
                        ],
                        cwd=root,
                        check=True,
                    )

                    result = subprocess.run(
                        ["bash", str(root / "scripts" / "release-check.sh")],
                        cwd=root.parent,
                        env=env,
                        text=True,
                        capture_output=True,
                        check=False,
                    )

                    self.assertNotEqual(0, result.returncode)
                    self.assertIn(
                        f"required release executable is not executable in git archive: {relative}",
                        result.stderr,
                    )


if __name__ == "__main__":
    unittest.main()
