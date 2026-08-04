"""Verified GitHub-backed profile catalog for the Game Host Console.

The official repository publishes a small index plus immutable-by-digest JSON
packages.  Catalog refreshes fail closed and fall back to the last verified
cache (or the catalog bundled with the release).  Installing a package never
executes downloaded content: it only persists a schema-checked profile and its
confined adapter declaration for activation on the next service start.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from jsonschema import Draft202012Validator
from registry import GameRegistry, RegistryError


DEFAULT_INDEX_URL = (
    "https://raw.githubusercontent.com/Stormxftw/"
    "hermes-game-host-console/main/catalog/index.json"
)
MAX_INDEX_BYTES = 512_000
MAX_PACKAGE_BYTES = 512_000
_GAME_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$")
_OFFICIAL_REPOSITORY = "https://github.com/Stormxftw/hermes-game-host-console"
_BUNDLED_IDS = frozenset(
    {
        "minecraft",
        "palworld",
        "valheim",
        "cs2",
        "terraria",
        "dont-starve-together",
        "satisfactory",
        "enshrouded",
        "sons-of-the-forest",
    }
)
_COMMUNITY_SCRIPT_SLOTS = {
    "service.start": ("start.sh",),
    "service.stop": ("stop.sh",),
    "service.restart": ("stop.sh", "start.sh"),
    "backup.create": ("backup.sh",),
}


class ProfileStoreError(ValueError):
    """Raised when catalog data is unavailable, untrusted, or invalid."""


def strict_json_loads(raw: str | bytes) -> Any:
    """Parse standards-compliant JSON while rejecting duplicate object keys."""

    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ProfileStoreError(f"duplicate JSON field: {key}")
            value[key] = item
        return value

    def reject_constant(value: str) -> Any:
        raise ProfileStoreError(f"non-finite JSON number is not allowed: {value}")

    return json.loads(raw, object_pairs_hook=object_pairs, parse_constant=reject_constant)


def _read_json(path: Path) -> Any:
    return strict_json_loads(path.read_text(encoding="utf-8"))


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, indent=2, sort_keys=True) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _safe_relative(value: str, *, label: str) -> Path:
    path = Path(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ProfileStoreError(f"{label} must be a confined relative path")
    return path


def _version_key(value: str) -> tuple[int, int, int, int, str]:
    base, separator, prerelease = value.partition("-")
    major, minor, patch = (int(part) for part in base.split("."))
    return major, minor, patch, 0 if separator else 1, prerelease


class ProfileStore:
    """Refresh, verify, cache, and activate official profile packages."""

    def __init__(
        self,
        *,
        cache_dir: Path,
        schemas_dir: Path,
        bundled_index: Path,
        index_url: str = DEFAULT_INDEX_URL,
        fetcher: Callable[[str, int], bytes] | None = None,
        refresh_ttl: int = 300,
        clock: Callable[[], float] = time.time,
        allow_test_urls: bool = False,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.schemas_dir = Path(schemas_dir)
        self.bundled_index = Path(bundled_index)
        self.index_url = index_url
        self.fetcher = fetcher or self._fetch_https
        self.refresh_ttl = refresh_ttl
        self.clock = clock
        self.allow_test_urls = allow_test_urls
        self._memory_index: dict[str, Any] | None = None
        self._memory_warning: str | None = None
        self._refreshed_at = 0.0
        self._profile_validator = Draft202012Validator(
            _read_json(self.schemas_dir / "game-control-profile.schema.json")
        )
        self._adapter_validator = Draft202012Validator(
            _read_json(self.schemas_dir / "game-adapter-config.schema.json")
        )
        self._validate_index_url(index_url)

    @property
    def index_cache_path(self) -> Path:
        return self.cache_dir / "index.json"

    @property
    def packages_dir(self) -> Path:
        return self.cache_dir / "packages"

    def _validate_index_url(self, url: str) -> None:
        if self.allow_test_urls:
            return
        parsed = urllib.parse.urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "raw.githubusercontent.com"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not parsed.path.startswith(
                "/Stormxftw/hermes-game-host-console/main/catalog/"
            )
        ):
            raise ProfileStoreError("profile catalog URL is not the approved GitHub source")

    def _fetch_https(self, url: str, limit: int) -> bytes:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "Hermes-Game-Host-Console/profile-store",
            },
        )
        with urllib.request.urlopen(request, timeout=8) as response:
            self._validate_index_url(response.geturl())
            content_type = response.headers.get_content_type()
            if content_type not in {"application/json", "text/plain"}:
                raise ProfileStoreError("profile catalog returned an unexpected content type")
            data = response.read(limit + 1)
        if len(data) > limit:
            raise ProfileStoreError("profile catalog response exceeds its size limit")
        return data

    def _decode_json(self, raw: bytes, *, label: str) -> dict[str, Any]:
        try:
            value = strict_json_loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError, ProfileStoreError) as exc:
            raise ProfileStoreError(f"{label} is not valid UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise ProfileStoreError(f"{label} must be a JSON object")
        return value

    def validate_index(self, index: dict[str, Any]) -> dict[str, Any]:
        if set(index) != {"schemaVersion", "repository", "games"}:
            raise ProfileStoreError("profile catalog has invalid top-level fields")
        if index.get("schemaVersion") != 1:
            raise ProfileStoreError("unsupported profile catalog schemaVersion")
        if index.get("repository") != _OFFICIAL_REPOSITORY:
            raise ProfileStoreError("profile catalog repository is not approved")
        games = index.get("games")
        if not isinstance(games, list):
            raise ProfileStoreError("profile catalog games must be an array")
        seen: set[str] = set()
        for position, entry in enumerate(games):
            if not isinstance(entry, dict):
                raise ProfileStoreError(f"profile catalog game {position} must be an object")
            required = {
                "id", "name", "description", "version", "packagePath",
                "sha256", "sizeBytes", "tags",
            }
            if set(entry) != required:
                raise ProfileStoreError(f"profile catalog game {position} has invalid fields")
            game_id = entry["id"]
            if not isinstance(game_id, str) or not _GAME_ID.fullmatch(game_id):
                raise ProfileStoreError("profile catalog contains an invalid game id")
            if game_id in seen:
                raise ProfileStoreError(f"duplicate profile catalog game: {game_id}")
            seen.add(game_id)
            if not isinstance(entry["name"], str) or not 1 <= len(entry["name"]) <= 80:
                raise ProfileStoreError(f"invalid name for catalog game {game_id}")
            if not isinstance(entry["description"], str) or len(entry["description"]) > 400:
                raise ProfileStoreError(f"invalid description for catalog game {game_id}")
            if not isinstance(entry["version"], str) or not _VERSION.fullmatch(entry["version"]):
                raise ProfileStoreError(f"invalid version for catalog game {game_id}")
            package_path = _safe_relative(entry["packagePath"], label="packagePath")
            if package_path.parts[:1] != ("packages",) or package_path.suffix != ".json":
                raise ProfileStoreError("packagePath must point beneath packages/")
            digest = entry["sha256"]
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ProfileStoreError(f"invalid digest for catalog game {game_id}")
            size = entry["sizeBytes"]
            if not isinstance(size, int) or not 1 <= size <= MAX_PACKAGE_BYTES:
                raise ProfileStoreError(f"invalid package size for catalog game {game_id}")
            tags = entry["tags"]
            if (
                not isinstance(tags, list)
                or len(tags) > 12
                or any(not isinstance(tag, str) or not 1 <= len(tag) <= 32 for tag in tags)
            ):
                raise ProfileStoreError(f"invalid tags for catalog game {game_id}")
            if len(tags) != len(set(tags)):
                raise ProfileStoreError(f"duplicate tags for catalog game {game_id}")
        return index

    def _reject_catalog_rollback(self, candidate: dict[str, Any]) -> None:
        if not self.index_cache_path.is_file():
            return
        try:
            cached = self.validate_index(_read_json(self.index_cache_path))
        except Exception:
            return
        cached_by_id = {entry["id"]: entry for entry in cached["games"]}
        for entry in candidate["games"]:
            previous = cached_by_id.get(entry["id"])
            if previous is None:
                continue
            if _version_key(entry["version"]) < _version_key(previous["version"]):
                raise ProfileStoreError(
                    f"profile catalog rollback rejected for {entry['id']}"
                )
            if (
                entry["version"] == previous["version"]
                and entry["sha256"] != previous["sha256"]
            ):
                raise ProfileStoreError(
                    f"profile catalog changed immutable version for {entry['id']}"
                )

    def refresh(self, *, force: bool = False) -> tuple[dict[str, Any], str, str | None]:
        now = self.clock()
        if (
            not force
            and self._memory_index is not None
            and now - self._refreshed_at < self.refresh_ttl
        ):
            return self._memory_index, "memory-cache", self._memory_warning
        error: str | None = None
        try:
            raw = self.fetcher(self.index_url, MAX_INDEX_BYTES)
            index = self.validate_index(self._decode_json(raw, label="profile catalog"))
            self._reject_catalog_rollback(index)
            _atomic_json(self.index_cache_path, index)
            self._memory_index = index
            self._memory_warning = None
            self._refreshed_at = now
            return index, "github", None
        except Exception as exc:
            error = str(exc)
        for path, source in (
            (self.index_cache_path, "disk-cache"),
            (self.bundled_index, "bundled"),
        ):
            try:
                index = self.validate_index(_read_json(path))
                self._memory_index = index
                self._memory_warning = error
                self._refreshed_at = now
                return index, source, error
            except Exception:
                continue
        raise ProfileStoreError(f"no verified profile catalog is available: {error}")

    def _entry(self, game_id: str, index: dict[str, Any]) -> dict[str, Any]:
        for entry in index["games"]:
            if entry["id"] == game_id:
                return entry
        raise ProfileStoreError(f"unknown store game: {game_id}")

    def validate_package(self, package: dict[str, Any], expected_id: str | None = None) -> dict[str, Any]:
        required = {"schemaVersion", "id", "version", "tags", "profile", "adapter"}
        if set(package) != required or package.get("schemaVersion") != 1:
            raise ProfileStoreError("profile package has invalid fields or schemaVersion")
        game_id = package.get("id")
        if not isinstance(game_id, str) or not _GAME_ID.fullmatch(game_id):
            raise ProfileStoreError("profile package has an invalid game id")
        if expected_id is not None and game_id != expected_id:
            raise ProfileStoreError("profile package id does not match catalog entry")
        if not isinstance(package.get("version"), str) or not _VERSION.fullmatch(package["version"]):
            raise ProfileStoreError("profile package has an invalid version")
        tags = package.get("tags")
        if (
            not isinstance(tags, list)
            or len(tags) > 12
            or any(not isinstance(tag, str) or not 1 <= len(tag) <= 32 for tag in tags)
        ):
            raise ProfileStoreError("profile package tags are invalid")
        if len(tags) != len(set(tags)):
            raise ProfileStoreError("profile package tags must be unique")
        profile = package.get("profile")
        adapter = package.get("adapter")
        if not isinstance(profile, dict) or not isinstance(adapter, dict):
            raise ProfileStoreError("profile package profile and adapter must be objects")
        errors = sorted(self._profile_validator.iter_errors(profile), key=lambda item: list(item.path))
        if errors:
            raise ProfileStoreError(f"profile schema validation failed: {errors[0].message}")
        adapter_document = {"games": {game_id: adapter}}
        errors = sorted(self._adapter_validator.iter_errors(adapter_document), key=lambda item: list(item.path))
        if errors:
            raise ProfileStoreError(f"adapter schema validation failed: {errors[0].message}")
        if profile["id"] != game_id:
            raise ProfileStoreError("package, profile, and adapter ids must agree")
        adapter_project = _safe_relative(
            adapter["projectDir"], label="adapter projectDir"
        )
        backup = adapter.get("backup")
        if backup is not None:
            backup_project = _safe_relative(
                backup["projectDir"], label="adapter backup projectDir"
            )
            if backup_project != adapter_project:
                raise ProfileStoreError(
                    "adapter backup projectDir must match adapter projectDir"
                )
            _safe_relative(backup["sourceDir"], label="adapter backup sourceDir")
            _safe_relative(backup["backupDir"], label="adapter backup backupDir")
        declared_actions = set(adapter["commands"]) | {"ui.refresh"}
        if adapter.get("propertyTypes"):
            declared_actions.add("property.set")
        control_ids: set[str] = set()
        for control in profile["controls"]:
            if control["id"] in control_ids:
                raise ProfileStoreError(f"duplicate control id: {control['id']}")
            control_ids.add(control["id"])
            binding = control["binding"]
            if binding["action"] not in declared_actions:
                raise ProfileStoreError(
                    f"control {control['id']} references undeclared action {binding['action']}"
                )
            if binding["action"] == "property.set" and binding["key"] not in adapter.get("propertyTypes", {}):
                raise ProfileStoreError(
                    f"control {control['id']} references undeclared property {binding['key']}"
                )
        for action, specs in adapter["commands"].items():
            for script_name, _timeout in specs:
                _safe_relative(script_name, label=f"adapter command {action}")
        if game_id not in _BUNDLED_IDS:
            expected_project = f"community/{game_id}"
            if adapter["projectDir"] != expected_project:
                raise ProfileStoreError(
                    f"community adapter projectDir must be {expected_project}"
                )
            if adapter.get("propertyTypes"):
                raise ProfileStoreError(
                    "community adapters cannot declare mutable properties"
                )
            for action, specs in adapter["commands"].items():
                expected_scripts = _COMMUNITY_SCRIPT_SLOTS.get(action)
                actual_scripts = tuple(spec[0] for spec in specs)
                if expected_scripts is None or actual_scripts != expected_scripts:
                    raise ProfileStoreError(
                        f"community adapter action {action} must use fixed script slots"
                    )
        with tempfile.TemporaryDirectory(prefix="profile-store-validation-") as tmp:
            root = Path(tmp)
            projects = root / "projects"
            profiles = root / "profiles"
            projects.mkdir()
            profiles.mkdir()
            (profiles / f"{game_id}.json").write_text(
                json.dumps(profile, sort_keys=True), encoding="utf-8"
            )
            adapter_path = root / "adapters.json"
            adapter_path.write_text(
                json.dumps({"games": {game_id: adapter}}, sort_keys=True),
                encoding="utf-8",
            )
            try:
                GameRegistry(
                    projects_root=projects,
                    profiles_dir=profiles,
                    adapter_config_path=adapter_path,
                    schema_dir=self.schemas_dir,
                )
            except RegistryError as exc:
                raise ProfileStoreError(
                    f"package is not runtime-compatible: {exc}"
                ) from exc
        return package

    def install(self, game_id: str) -> dict[str, Any]:
        if not _GAME_ID.fullmatch(game_id):
            raise ProfileStoreError("invalid game id")
        index, source, warning = self.refresh(force=True)
        entry = self._entry(game_id, index)
        package_url = urllib.parse.urljoin(self.index_url, entry["packagePath"])
        if not self.allow_test_urls:
            self._validate_index_url(package_url)
        raw = self.fetcher(package_url, MAX_PACKAGE_BYTES)
        if len(raw) != entry["sizeBytes"]:
            raise ProfileStoreError("profile package size does not match catalog")
        if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            raise ProfileStoreError("profile package digest does not match catalog")
        package = self.validate_package(
            self._decode_json(raw, label="profile package"), expected_id=game_id
        )
        profile = package["profile"]
        if (
            package["version"] != entry["version"]
            or package["tags"] != entry["tags"]
            or profile["name"] != entry["name"]
            or profile.get("description", "") != entry["description"]
        ):
            raise ProfileStoreError("profile package metadata does not match catalog")
        _atomic_json(self.packages_dir / f"{game_id}.json", package)
        return {"package": package, "source": source, "warning": warning}

    def is_downloaded(self, game_id: str) -> bool:
        return bool(_GAME_ID.fullmatch(game_id)) and (
            self.packages_dir / f"{game_id}.json"
        ).is_file()

    def installed_packages(
        self, *, active_ids: set[str] | None = None
    ) -> list[dict[str, Any]]:
        packages: list[dict[str, Any]] = []
        if not self.packages_dir.is_dir():
            return packages
        for path in sorted(self.packages_dir.glob("*.json")):
            if active_ids is not None and path.stem not in active_ids:
                continue
            try:
                packages.append(self.validate_package(_read_json(path), expected_id=path.stem))
            except Exception as exc:
                raise ProfileStoreError(f"installed package {path.name} is invalid: {exc}") from exc
        return packages

    def materialize(
        self,
        *,
        bundled_profiles: Path,
        bundled_adapters: Path,
        active_ids: set[str] | None = None,
    ) -> tuple[Path, Path]:
        """Create a deterministic runtime registry with installed packages overlaid."""
        packages = self.installed_packages(active_ids=active_ids)
        if not packages:
            return Path(bundled_profiles), Path(bundled_adapters)
        runtime = self.cache_dir / "runtime"
        profiles = runtime / "profiles"
        if runtime.exists():
            shutil.rmtree(runtime)
        shutil.copytree(bundled_profiles, profiles)
        adapters = _read_json(bundled_adapters)
        for package in packages:
            game_id = package["id"]
            _atomic_json(profiles / f"{game_id}.json", package["profile"])
            adapters["games"][game_id] = package["adapter"]
        adapter_path = runtime / "game_adapters.json"
        _atomic_json(adapter_path, adapters)
        return profiles, adapter_path
