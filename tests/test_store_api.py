import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

import app
from store import InstalledStore

_ADAPTER = {
    "games": {
        "alpha": {
            "projectDir": "alpha-server",
            "commands": {
                "service.start": [["start.sh", 60]],
                "service.stop": [["stop.sh", 60]],
            },
            "propertyTypes": {},
            "statusCollector": "process_only",
            "processSearch": "alpha-server",
            "defaultPort": 7777,
            "portProtocol": "tcp",
        },
        "beta": {
            "projectDir": "beta-server",
            "commands": {
                "service.start": [["start.sh", 60]],
                "service.stop": [["stop.sh", 60]],
            },
            "propertyTypes": {},
            "statusCollector": "process_only",
            "processSearch": "beta-server",
            "defaultPort": 7788,
            "portProtocol": "tcp",
        },
    }
}


def _write_tree(root: Path) -> None:
    projects = root / "projects"
    profiles = root / "profiles"
    projects.mkdir(parents=True)
    profiles.mkdir()
    (root / "game_adapters.json").write_text(json.dumps(_ADAPTER), encoding="utf-8")
    for game_id in ("alpha", "beta"):
        (profiles / f"{game_id}.json").write_text(
            json.dumps(
                {
                    "schemaVersion": "1.0",
                    "id": game_id,
                    "name": game_id.title(),
                    "controls": [
                        {
                            "id": "start",
                            "kind": "button",
                            "label": "Start",
                            "risk": "service",
                            "binding": {"action": "service.start"},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
    # alpha is provisioned; beta is not.
    (projects / "alpha-server").mkdir()
    for name in ("start.sh", "stop.sh"):
        script = projects / "alpha-server" / name
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        script.chmod(0o755)


def _collect(**kwargs):
    on = kwargs["adapter"]["processSearch"] == "alpha-server"
    return {
        "id": kwargs["game_id"],
        "name": kwargs["name"],
        "state": "running_ready" if on else "stopped",
        "online": on,
        "process": {"ok": True, "running": on, "pid": 1 if on else None, "error": None},
        "listeners": [],
        "query": {"attempted": False, "ok": None, "error": None},
        "connect": {"local": None, "lan": None, "public": None},
    }


class StoreApiTests(unittest.TestCase):
    def _start(self, root: Path):
        server = app.create_server(
            host="127.0.0.1",
            port=0,
            projects_root=root / "projects",
            profiles_dir=root / "profiles",
            audit_path=root / "audit.jsonl",
            adapter_config_path=root / "game_adapters.json",
            telemetry_collector=_collect,
            installed_store=app.InstalledStore(root / "installed.json", seed={"alpha"}),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread, f"http://127.0.0.1:{server.server_address[1]}"

    def _get(self, base, path):
        with urllib.request.urlopen(f"{base}{path}", timeout=3) as response:
            return json.load(response)

    def _post(self, base, path, body):
        request = urllib.request.Request(
            f"{base}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.load(response)

    def test_installed_store_roundtrip_is_persistent_and_newly_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "installed.json"
            store = InstalledStore(path, seed={"alpha"})
            self.assertTrue(store.is_installed("alpha"))
            store.install("beta")
            store.uninstall("alpha")
            reloaded = InstalledStore(path)
            self.assertEqual({"beta"}, reloaded.installed_ids())
            self.assertTrue(path.is_file())

    def test_catalog_and_store_respect_installed_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_tree(root)
            server, thread, base = self._start(root)
            try:
                catalog = self._get(base, "/api/controls")
                by_id = {g["id"]: g for g in catalog["games"]}
                self.assertTrue(by_id["alpha"]["installed"])
                self.assertFalse(by_id["beta"]["installed"])
                self.assertEqual(["alpha"], catalog["installedIds"])

                store = self._get(base, "/api/store")
                # beta is available (not installed); alpha is not listed as available
                self.assertEqual(["beta"], [g["id"] for g in store["store"]])
                self.assertEqual(["alpha"], store["installed"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_install_scaffolds_project_and_uninstall_removes_flag_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_tree(root)
            server, thread, base = self._start(root)
            try:
                status, result = self._post(
                    base, "/api/store/install", {"gameId": "beta"}
                )
                self.assertEqual(200, status)
                self.assertTrue(result["installed"])
                self.assertEqual(
                    ["alpha", "beta"], sorted(result["installedIds"])
                )
                self.assertTrue((root / "projects" / "beta-server").is_dir())
                self.assertTrue(
                    (root / "projects" / "beta-server" / "PROVISION.md").is_file()
                )

                status, result = self._post(
                    base, "/api/store/uninstall", {"gameId": "beta"}
                )
                self.assertEqual(200, status)
                self.assertFalse(result["installed"])
                # Uninstall keeps the scaffolding; it only drops the flag.
                self.assertTrue((root / "projects" / "beta-server").is_dir())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_install_rejects_unknown_or_malformed_game(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_tree(root)
            server, thread, base = self._start(root)
            try:
                for bad in ("does-not-exist", "../escape"):
                    with self.subTest(game=bad), self.assertRaises(
                        urllib.error.HTTPError
                    ) as err:
                        self._post(base, "/api/store/install", {"gameId": bad})
                    self.assertEqual(400, err.exception.code)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
