import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

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


class ControlApiTests(unittest.TestCase):
    def test_http_api_exposes_catalog_and_plan_without_mutating(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            profiles = root / "profiles"
            minecraft = projects / "minecraft-server"
            minecraft.mkdir(parents=True)
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
                            "actor": "api-test",
                        }
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=3) as response:
                    plan = json.load(response)
                self.assertEqual(18, plan["proposedValue"])
                self.assertTrue(plan["requiresConfirmation"])
                self.assertEqual("max-players=10\n", properties.read_text(encoding="utf-8"))

                apply_request = urllib.request.Request(
                    f"{base}/api/control/apply",
                    data=json.dumps(
                        {
                            "planId": plan["planId"],
                            "planDigest": plan["planDigest"],
                            "confirmed": True,
                            "actor": "api-test",
                        }
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(apply_request, timeout=3) as response:
                    applied = json.load(response)
                self.assertTrue(applied["ok"])
                self.assertEqual("max-players=18\n", properties.read_text(encoding="utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
