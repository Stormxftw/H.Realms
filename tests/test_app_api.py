import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import app

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
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        script.chmod(0o755)


class ControlApiTests(unittest.TestCase):
    def test_create_server_refuses_non_loopback_hosts_before_binding(self):
        with mock.patch.object(app, "ThreadingHTTPServer") as server_factory:
            for host in ("0.0.0.0", "192.168.1.25", "::", "2001:db8::25", "dashboard.local"):
                with self.subTest(host=host):
                    with self.assertRaisesRegex(ValueError, "loopback"):
                        app.create_server(host=host, port=5057)

        server_factory.assert_not_called()

    def test_default_host_cannot_be_weakened_by_environment(self):
        env = dict(os.environ, DASHBOARD_HOST="0.0.0.0")
        result = subprocess.run(
            [sys.executable, "-c", "import app; print(app.DEFAULT_HOST)"],
            cwd=Path(__file__).parents[1],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )

        self.assertEqual("127.0.0.1", result.stdout.strip())

    def test_http_api_exposes_catalog_and_plan_without_mutating(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            profiles = root / "profiles"
            minecraft = projects / "minecraft-server"
            minecraft.mkdir(parents=True)
            _create_executable_adapter_scripts(minecraft)
            profiles.mkdir()
            properties = minecraft / "server.properties"
            properties.write_text("max-players=10\n", encoding="utf-8")
            adapter_path = root / "game_adapters.json"
            adapter_path.write_text(json.dumps(_MC_ADAPTER))
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
                                "min": 1,
                                "max": 50,
                                "step": 1,
                                "risk": "configuration",
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
            server = app.create_server(
                host="127.0.0.1",
                port=0,
                projects_root=projects,
                profiles_dir=profiles,
                audit_path=root / "audit.jsonl",
                adapter_config_path=adapter_path,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with urllib.request.urlopen(f"{base}/api/controls", timeout=3) as response:
                    catalog = json.load(response)
                self.assertEqual("minecraft", catalog["games"][0]["id"])

                request = urllib.request.Request(
                    f"{base}/api/control/plan",
                    data=json.dumps(
                        {
                            "gameId": "minecraft",
                            "controlId": "max-players",
                            "value": 18,
                            "actor": "spoofed-remote-user",
                        }
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=3) as response:
                    plan = json.load(response)
                self.assertEqual(18, plan["proposedValue"])
                self.assertEqual("local-console", plan["actor"])
                self.assertNotIn("spoofed-remote-user", json.dumps(plan))
                self.assertTrue(plan["requiresConfirmation"])
                self.assertEqual("max-players=10\n", properties.read_text(encoding="utf-8"))

                with self.assertRaises(urllib.error.HTTPError) as missing_archive:
                    urllib.request.urlopen(
                        f"{base}/project-zim-architecture-inventory.zip",
                        timeout=3,
                    )
                self.assertEqual(404, missing_archive.exception.code)

                local_apply_request = urllib.request.Request(
                    f"{base}/api/control/apply",
                    data=json.dumps(
                        {
                            "planId": plan["planId"],
                            "planDigest": plan["planDigest"],
                            "confirmed": True,
                            "actor": "spoofed-remote-user",
                            "source": "spoofed-local-source",
                        }
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(local_apply_request, timeout=3) as response:
                    local_applied = json.load(response)
                self.assertEqual("local-console", local_applied["actor"])
                self.assertEqual("local-console", local_applied["plannedBy"])
                self.assertNotIn("spoofed", json.dumps(local_applied))
                self.assertEqual("max-players=18\n", properties.read_text(encoding="utf-8"))

                bridge_request = urllib.request.Request(
                    f"{base}/api/bridge/control/plan",
                    data=json.dumps(
                        {
                            "gameId": "minecraft",
                            "controlId": "max-players",
                            "value": 20,
                            "actor": "another-spoofed-user",
                            "source": "spoofed-bridge-source",
                        }
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(bridge_request, timeout=3) as response:
                    plan = json.load(response)
                self.assertEqual("hermes-authenticated-bridge", plan["actor"])
                self.assertNotIn("spoofed", json.dumps(plan))

                missing_digest_request = urllib.request.Request(
                    f"{base}/api/bridge/control/apply",
                    data=json.dumps(
                        {
                            "planId": plan["planId"],
                            "confirmed": True,
                            "actor": "spoofed-remote-user",
                        }
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    urllib.request.urlopen(missing_digest_request, timeout=3)
                self.assertEqual(400, rejected.exception.code)
                self.assertIn("planDigest is required", rejected.exception.read().decode("utf-8"))
                self.assertEqual("max-players=18\n", properties.read_text(encoding="utf-8"))

                for confirmation in (None, "true"):
                    rejected_body = {
                        "planId": plan["planId"],
                        "planDigest": plan["planDigest"],
                        "actor": "spoofed-remote-user",
                    }
                    if confirmation is not None:
                        rejected_body["confirmed"] = confirmation
                    confirmation_request = urllib.request.Request(
                        f"{base}/api/bridge/control/apply",
                        data=json.dumps(rejected_body).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with self.subTest(confirmation=confirmation):
                        with self.assertRaises(urllib.error.HTTPError) as rejected:
                            urllib.request.urlopen(confirmation_request, timeout=3)
                        self.assertEqual(400, rejected.exception.code)
                        self.assertIn(
                            "confirmation required",
                            rejected.exception.read().decode("utf-8"),
                        )
                self.assertEqual("max-players=18\n", properties.read_text(encoding="utf-8"))

                apply_request = urllib.request.Request(
                    f"{base}/api/bridge/control/apply",
                    data=json.dumps(
                        {
                            "planId": plan["planId"],
                            "planDigest": plan["planDigest"],
                            "confirmed": True,
                            "actor": "spoofed-remote-user",
                            "source": "spoofed-bridge-source",
                        }
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(apply_request, timeout=3) as response:
                    applied = json.load(response)
                self.assertTrue(applied["ok"])
                self.assertEqual("hermes-authenticated-bridge", applied["actor"])
                self.assertEqual("hermes-authenticated-bridge", applied["plannedBy"])
                self.assertNotIn("spoofed", json.dumps(applied))
                self.assertEqual("max-players=20\n", properties.read_text(encoding="utf-8"))
                audit = [
                    json.loads(line)
                    for line in (root / "audit.jsonl").read_text(encoding="utf-8").splitlines()
                ]
                self.assertEqual(
                    ["local-console", "hermes-authenticated-bridge"],
                    [row["actor"] for row in audit],
                )
                self.assertEqual(
                    ["local-console", "hermes-authenticated-bridge"],
                    [row["plannedBy"] for row in audit],
                )
                self.assertNotIn("spoofed", json.dumps(audit))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
