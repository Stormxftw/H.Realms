import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from profile_store import ProfileStore, ProfileStoreError

ROOT = Path(__file__).resolve().parents[1]


def _package(game_id: str = "alpha") -> dict:
    return {
        "schemaVersion": 1,
        "id": game_id,
        "version": "1.0.0",
        "tags": ["test"],
        "profile": {
            "schemaVersion": "1.0",
            "id": game_id,
            "name": game_id.title(),
            "description": "Test profile",
            "controls": [
                {
                    "id": "start",
                    "kind": "button",
                    "label": "Start",
                    "risk": "service",
                    "binding": {"action": "service.start"},
                }
            ],
        },
        "adapter": {
            "projectDir": f"community/{game_id}",
            "commands": {"service.start": [["start.sh", 60]]},
            "propertyTypes": {},
            "statusCollector": "process_only",
            "processSearch": f"{game_id}-server",
            "defaultPort": 7777,
            "portProtocol": "tcp",
        },
    }


def _bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _index(raw_package: bytes, game_id: str = "alpha") -> dict:
    return {
        "schemaVersion": 1,
        "repository": "https://github.com/Stormxftw/hermes-game-host-console",
        "games": [
            {
                "id": game_id,
                "name": game_id.title(),
                "description": "Test profile",
                "version": "1.0.0",
                "packagePath": f"packages/{game_id}.json",
                "sha256": hashlib.sha256(raw_package).hexdigest(),
                "sizeBytes": len(raw_package),
                "tags": ["test"],
            }
        ],
    }


class ProfileStoreTests(unittest.TestCase):
    def _store(self, root: Path, responses: dict[str, bytes], bundled: dict | None = None):
        bundled_path = root / "bundled-index.json"
        if bundled is not None:
            bundled_path.write_bytes(_bytes(bundled))

        def fetch(url: str, limit: int) -> bytes:
            value = responses[url]
            if len(value) > limit:
                raise ProfileStoreError("too large")
            return value

        return ProfileStore(
            cache_dir=root / "cache",
            schemas_dir=ROOT / "schemas",
            bundled_index=bundled_path,
            index_url="test://catalog/index.json",
            fetcher=fetch,
            allow_test_urls=True,
            refresh_ttl=0,
        )

    def test_refresh_uses_verified_github_index_and_persists_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_package = _bytes(_package())
            index = _index(raw_package)
            store = self._store(root, {"test://catalog/index.json": _bytes(index)})

            actual, source, warning = store.refresh(force=True)

            self.assertEqual(index, actual)
            self.assertEqual("github", source)
            self.assertIsNone(warning)
            self.assertEqual(index, json.loads(store.index_cache_path.read_text()))

    def test_refresh_falls_back_to_verified_bundled_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index = _index(_bytes(_package()))

            def broken(_url: str, _limit: int) -> bytes:
                raise OSError("offline")

            bundled_path = root / "bundled.json"
            bundled_path.write_bytes(_bytes(index))
            store = ProfileStore(
                cache_dir=root / "cache",
                schemas_dir=ROOT / "schemas",
                bundled_index=bundled_path,
                index_url="test://catalog/index.json",
                fetcher=broken,
                allow_test_urls=True,
            )

            actual, source, warning = store.refresh(force=True)

            self.assertEqual(index, actual)
            self.assertEqual("bundled", source)
            self.assertIsNotNone(warning)
            self.assertIn("offline", warning or "")
            _actual, memory_source, memory_warning = store.refresh()
            self.assertEqual("memory-cache", memory_source)
            self.assertIn("offline", memory_warning or "")

    def test_refresh_rejects_rollback_and_same_version_content_rewrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current_raw = _bytes(_package())
            current = _index(current_raw)
            store = self._store(root, {"test://catalog/index.json": _bytes(current)})
            store.refresh(force=True)

            rolled_back = json.loads(json.dumps(current))
            rolled_back["games"][0]["version"] = "0.9.0"
            store.fetcher = lambda _url, _limit: _bytes(rolled_back)
            actual, source, warning = store.refresh(force=True)
            self.assertEqual(current, actual)
            self.assertEqual("disk-cache", source)
            self.assertIn("rollback", warning or "")

            rewritten = json.loads(json.dumps(current))
            rewritten["games"][0]["sha256"] = "f" * 64
            store.fetcher = lambda _url, _limit: _bytes(rewritten)
            actual, source, warning = store.refresh(force=True)
            self.assertEqual(current, actual)
            self.assertEqual("disk-cache", source)
            self.assertIn("immutable version", warning or "")

    def test_install_verifies_digest_and_materializes_profile_and_adapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = _package()
            raw_package = _bytes(package)
            index = _index(raw_package)
            responses = {
                "test://catalog/index.json": _bytes(index),
                "packages/alpha.json": raw_package,
            }
            store = self._store(root, responses)
            bundled_profiles = root / "profiles"
            bundled_profiles.mkdir()
            (bundled_profiles / "_template.json").write_text("{}")
            bundled_adapters = root / "adapters.json"
            bundled_adapters.write_text('{"games": {}}')

            result = store.install("alpha")
            profiles, adapters = store.materialize(
                bundled_profiles=bundled_profiles,
                bundled_adapters=bundled_adapters,
            )

            self.assertEqual("alpha", result["package"]["id"])
            self.assertEqual(package["profile"], json.loads((profiles / "alpha.json").read_text()))
            self.assertEqual(
                package["adapter"], json.loads(adapters.read_text())["games"]["alpha"]
            )

    def test_install_rejects_digest_mismatch_without_persisting_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_package = _bytes(_package())
            index = _index(raw_package)
            index["games"][0]["sha256"] = "0" * 64
            store = self._store(
                root,
                {
                    "test://catalog/index.json": _bytes(index),
                    "packages/alpha.json": raw_package,
                },
            )

            with self.assertRaisesRegex(ProfileStoreError, "digest"):
                store.install("alpha")

            self.assertFalse((store.packages_dir / "alpha.json").exists())

    def test_inactive_corrupt_package_does_not_break_runtime_materialization(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp), {})
            store.packages_dir.mkdir(parents=True)
            (store.packages_dir / "inactive.json").write_text("{not-json")
            profiles, adapters = store.materialize(
                bundled_profiles=ROOT / "game_profiles",
                bundled_adapters=ROOT / "game_adapters.json",
                active_ids=set(),
            )
            self.assertEqual(ROOT / "game_profiles", profiles)
            self.assertEqual(ROOT / "game_adapters.json", adapters)

    def test_package_rejects_undeclared_actions_and_path_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp), {})
            bad_action = _package()
            bad_action["profile"]["controls"][0]["binding"]["action"] = "service.stop"
            with self.assertRaisesRegex(ProfileStoreError, "undeclared action"):
                store.validate_package(bad_action)

            bad_path = _package()
            bad_path["adapter"]["commands"]["service.start"][0][0] = "../escape.sh"
            with self.assertRaisesRegex(ProfileStoreError, "confined relative"):
                store.validate_package(bad_path)

    def test_strict_json_rejects_duplicate_fields_and_nonfinite_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp), {})
            with self.assertRaises(ProfileStoreError):
                store._decode_json(b'{"id":"alpha","id":"beta"}', label="package")
            with self.assertRaises(ProfileStoreError):
                store._decode_json(b'{"value":NaN}', label="package")

    def test_community_package_can_only_select_fixed_project_and_script_slots(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp), {})
            arbitrary_project = _package()
            arbitrary_project["adapter"]["projectDir"] = "existing-sensitive-server"
            with self.assertRaisesRegex(ProfileStoreError, "projectDir"):
                store.validate_package(arbitrary_project)

            arbitrary_script = _package()
            arbitrary_script["adapter"]["commands"]["service.start"] = [["custom.sh", 60]]
            with self.assertRaisesRegex(ProfileStoreError, "fixed script slots"):
                store.validate_package(arbitrary_script)

            mutable_property = _package()
            mutable_property["adapter"]["propertyTypes"] = {"admin.password": "string"}
            with self.assertRaisesRegex(ProfileStoreError, "mutable properties"):
                store.validate_package(mutable_property)

    def test_runtime_semantics_and_backup_root_are_validated_before_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp), {})
            minecraft = json.loads(
                (ROOT / "catalog" / "packages" / "minecraft.json").read_text(
                    encoding="utf-8"
                )
            )

            understated = json.loads(json.dumps(minecraft))
            next(
                control
                for control in understated["profile"]["controls"]
                if control["binding"]["action"] == "service.start"
            )["risk"] = "read-only"
            with self.assertRaisesRegex(ProfileStoreError, "runtime-compatible"):
                store.validate_package(understated)

            invalid_range = json.loads(json.dumps(minecraft))
            slider = next(
                control
                for control in invalid_range["profile"]["controls"]
                if "min" in control
            )
            slider["min"], slider["max"] = 10, 1
            with self.assertRaisesRegex(ProfileStoreError, "runtime-compatible"):
                store.validate_package(invalid_range)

            escaped_backup = _package()
            escaped_backup["adapter"]["backup"] = {
                "projectDir": "/",
                "sourceDir": "saved",
                "backupDir": "backups",
            }
            with self.assertRaisesRegex(ProfileStoreError, "confined relative"):
                store.validate_package(escaped_backup)

    def test_fetch_rejects_cross_origin_redirect(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(
                cache_dir=Path(tmp),
                schemas_dir=ROOT / "schemas",
                bundled_index=ROOT / "catalog" / "index.json",
            )
            response = mock.MagicMock()
            response.__enter__.return_value = response
            response.geturl.return_value = "https://example.com/redirected-index.json"
            response.headers.get_content_type.return_value = "application/json"
            response.read.return_value = b"{}"
            with mock.patch("profile_store.urllib.request.urlopen", return_value=response):
                with self.assertRaisesRegex(ProfileStoreError, "approved GitHub source"):
                    store._fetch_https(store.index_url, 1024)

    def test_production_url_is_pinned_to_official_raw_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ProfileStoreError, "approved GitHub source"):
                ProfileStore(
                    cache_dir=Path(tmp),
                    schemas_dir=ROOT / "schemas",
                    bundled_index=ROOT / "catalog" / "index.json",
                    index_url="https://example.com/catalog/index.json",
                )


if __name__ == "__main__":
    unittest.main()
