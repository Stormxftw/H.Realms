from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError


RISK_LEVELS = (
    "read-only",
    "safe",
    "safe-mutation",
    "configuration",
    "service",
    "disruptive",
)
ACTION_POLICIES = {
    "ui.refresh": ("read-only", False),
    "property.set": ("configuration", True),
    "service.start": ("service", True),
    "service.stop": ("disruptive", True),
    "service.restart": ("disruptive", True),
    "backup.create": ("safe-mutation", True),
}


class RegistryError(ValueError):
    """Raised when registry input cannot be validated safely."""


class _ObjectPairs(list[tuple[str, Any]]):
    """Marker preserving JSON object pairs until duplicates are checked."""


def _field_path(parent: str, field: str) -> str:
    if field and all(character.isalnum() or character in "_-" for character in field):
        return f"{parent}.{field}"
    return f"{parent}[{json.dumps(field)}]"


def _materialize_json(value: Any, *, path: str) -> Any:
    if isinstance(value, _ObjectPairs):
        result: dict[str, Any] = {}
        for key, item in value:
            field_path = _field_path(path, key)
            if key in result:
                raise RegistryError(f"{field_path}: duplicate field '{key}'")
            result[key] = _materialize_json(item, path=field_path)
        return result
    if isinstance(value, list):
        return [
            _materialize_json(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    return value


def _load_json(path: Path) -> Any:
    try:
        parsed = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_ObjectPairs,
        )
        return _materialize_json(parsed, path="$")
    except json.JSONDecodeError as exc:
        raise RegistryError(
            f"{path}: invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    except RegistryError as exc:
        raise RegistryError(f"{path}: {exc}") from exc


class GameRegistry:
    """Validated adapter and profile registry used by the control engine."""

    def __init__(
        self,
        *,
        projects_root: Path,
        profiles_dir: Path,
        adapter_config_path: Path,
        schema_dir: Path | None = None,
    ) -> None:
        self.projects_root = Path(projects_root)
        self.profiles_dir = Path(profiles_dir)
        self.adapter_config_path = Path(adapter_config_path)
        self.schema_dir = Path(schema_dir) if schema_dir else Path(__file__).parent / "schemas"

        adapter_schema = self._load_schema("game-adapter-config.schema.json")
        profile_schema = self._load_schema("game-control-profile.schema.json")
        data = _load_json(self.adapter_config_path)
        self._validate_document(self.adapter_config_path, data, adapter_schema)
        self.adapters: dict[str, Any] = data["games"]
        projects_root = self.projects_root.resolve()
        self.project_dirs: dict[str, Path] = {}
        self.commands: dict[str, dict[str, list[tuple[Path, Path, int]]]] = {}
        for game_id, adapter in self.adapters.items():
            configured = Path(adapter["projectDir"])
            try:
                resolved = (projects_root / configured).resolve()
            except (OSError, RuntimeError) as exc:
                raise RegistryError(
                    f"{self.adapter_config_path}: $.games.{game_id}.projectDir: "
                    "must resolve beneath PROJECTS_ROOT"
                ) from exc
            if configured.is_absolute() or not resolved.is_relative_to(projects_root):
                raise RegistryError(
                    f"{self.adapter_config_path}: $.games.{game_id}.projectDir: "
                    "must resolve beneath PROJECTS_ROOT"
                )
            self.project_dirs[game_id] = resolved
            self.commands[game_id] = {}
            for action, specs in adapter["commands"].items():
                resolved_specs: list[tuple[Path, Path, int]] = []
                for index, (script_name, timeout) in enumerate(specs):
                    configured_script = Path(script_name)
                    try:
                        resolved_script = (resolved / configured_script).resolve()
                    except (OSError, RuntimeError) as exc:
                        raise RegistryError(
                            f"{self.adapter_config_path}: $.games.{game_id}.commands."
                            f"{action}[{index}][0]: must resolve beneath game directory"
                        ) from exc
                    if configured_script.is_absolute() or not resolved_script.is_relative_to(resolved):
                        raise RegistryError(
                            f"{self.adapter_config_path}: $.games.{game_id}.commands."
                            f"{action}[{index}][0]: must resolve beneath game directory"
                        )
                    script_path = resolved / configured_script
                    try:
                        mode = script_path.lstat().st_mode
                    except OSError:
                        mode = 0
                    if (
                        script_path.is_symlink()
                        or not stat.S_ISREG(mode)
                        or not mode & 0o111
                    ):
                        raise RegistryError(
                            f"{self.adapter_config_path}: $.games.{game_id}.commands."
                            f"{action}[{index}][0]: required action script must be a "
                            "regular executable non-symlink file"
                        )
                    resolved_specs.append((resolved / configured_script, resolved_script, timeout))
                self.commands[game_id][action] = resolved_specs

        self.profiles: dict[str, dict[str, Any]] = {}
        for path in sorted(self.profiles_dir.glob("*.json")):
            if path.name.startswith("_"):
                continue
            profile = _load_json(path)
            self._validate_document(path, profile, profile_schema)
            if profile["id"] != path.stem:
                raise RegistryError(
                    f"{path}: $.id: profile id '{profile['id']}' "
                    f"must match filename id '{path.stem}'"
                )
            seen_control_ids: set[str] = set()
            for index, control in enumerate(profile["controls"]):
                control_id = control["id"]
                if control_id in seen_control_ids:
                    raise RegistryError(
                        f"{path}: $.controls[{index}].id: duplicate control id '{control_id}'"
                    )
                seen_control_ids.add(control_id)
                minimum = control.get("min")
                maximum = control.get("max")
                if minimum is not None and maximum is not None and maximum < minimum:
                    raise RegistryError(
                        f"{path}: $.controls[{index}].max: "
                        "must be greater than or equal to min"
                    )
            self.profiles[path.stem] = profile

        missing_adapters = sorted(set(self.profiles) - set(self.adapters))
        missing_profiles = sorted(set(self.adapters) - set(self.profiles))
        pair_errors = [
            *(f"missing adapter for profile '{game_id}'" for game_id in missing_adapters),
            *(f"missing profile for adapter '{game_id}'" for game_id in missing_profiles),
        ]
        if pair_errors:
            raise RegistryError(f"registry: {'; '.join(pair_errors)}")

        for game_id, profile in self.profiles.items():
            profile_path = self.profiles_dir / f"{game_id}.json"
            declared_actions = set(self.commands[game_id]) | {"ui.refresh"}
            if self.adapters[game_id].get("propertyTypes"):
                declared_actions.add("property.set")
            for control_index, control in enumerate(profile["controls"]):
                action = control["binding"]["action"]
                if action not in declared_actions:
                    raise RegistryError(
                        f"{profile_path}: $.controls[{control_index}].binding.action: "
                        f"action '{action}' is not declared by adapter '{game_id}'"
                    )
                if action == "property.set":
                    key = control["binding"]["key"]
                    if key not in self.adapters[game_id].get("propertyTypes", {}):
                        raise RegistryError(
                            f"{profile_path}: $.controls[{control_index}].binding.key: "
                            f"property key '{key}' is not declared by adapter '{game_id}'"
                        )
                backend_risk = ACTION_POLICIES[action][0]
                if RISK_LEVELS.index(control["risk"]) < RISK_LEVELS.index(backend_risk):
                    raise RegistryError(
                        f"{profile_path}: $.controls[{control_index}].risk: "
                        f"action '{action}' requires backend risk '{backend_risk}'"
                    )


    def _load_schema(self, name: str) -> dict[str, Any]:
        path = self.schema_dir / name
        schema = _load_json(path)
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            field = getattr(exc, "json_path", "$")
            raise RegistryError(f"{path}: invalid schema at {field}: {exc.message}") from exc
        return schema

    @staticmethod
    def _validate_document(path: Path, document: Any, schema: dict[str, Any]) -> None:
        error: ValidationError | None = next(
            iter(sorted(Draft202012Validator(schema).iter_errors(document), key=lambda item: list(item.path))),
            None,
        )
        if error is not None:
            raise RegistryError(f"{path}: {error.json_path}: {error.message}")
