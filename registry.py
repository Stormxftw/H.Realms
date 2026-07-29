from __future__ import annotations

import copy
import json
import os
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
PROPERTY_CONTROL_KINDS = {
    "integer": frozenset({"slider", "number"}),
    "boolean": frozenset({"switch"}),
    "string": frozenset({"text", "select"}),
}


class RegistryError(ValueError):
    """Raised when registry input cannot be validated safely."""


class _ObjectPairs(list[tuple[str, Any]]):
    """Marker preserving JSON object pairs until duplicates are checked."""


class _NonFiniteJsonConstant(ValueError):
    """Raised when Python's JSON decoder encounters a non-standard number."""


def _reject_non_finite_constant(value: str) -> Any:
    raise _NonFiniteJsonConstant(f"non-finite numeric constant '{value}'")


def _field_path(parent: str, field: str) -> str:
    if field and all(character.isalnum() or character in "_.-" for character in field):
        return f"{parent}.{field}"
    return f"{parent}[{json.dumps(field)}]"


def _json_path(parts: Any) -> str:
    path = "$"
    for part in parts:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path = _field_path(path, str(part))
    return path


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
            parse_constant=_reject_non_finite_constant,
        )
        return _materialize_json(parsed, path="$")
    except json.JSONDecodeError as exc:
        raise RegistryError(
            f"{path}: invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    except _NonFiniteJsonConstant as exc:
        raise RegistryError(f"{path}: invalid JSON: {exc}") from exc
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
        self.projects_root = Path(os.path.abspath(projects_root))
        self.profiles_dir = Path(profiles_dir)
        self.adapter_config_path = Path(adapter_config_path)
        self.schema_dir = Path(schema_dir) if schema_dir else Path(__file__).parent / "schemas"

        adapter_schema = self._load_schema("game-adapter-config.schema.json")
        profile_schema = self._load_schema("game-control-profile.schema.json")
        data = _load_json(self.adapter_config_path)
        self._validate_document(self.adapter_config_path, data, adapter_schema)
        self._adapters: dict[str, Any] = copy.deepcopy(data["games"])
        projects_root = self.projects_root.resolve()
        self._resolved_projects_root = projects_root
        self.project_paths: dict[str, Path] = {}
        self.project_dirs: dict[str, Path] = {}
        self.commands: dict[str, dict[str, list[tuple[Path, Path, int]]]] = {}
        for game_id, adapter in self._adapters.items():
            configured = Path(adapter["projectDir"])
            try:
                resolved = (projects_root / configured).resolve()
            except (OSError, RuntimeError) as exc:
                raise RegistryError(
                    f"{self.adapter_config_path}: $.games.{game_id}.projectDir: "
                    "must resolve beneath PROJECTS_ROOT"
                ) from exc
            if (
                configured.is_absolute()
                or resolved == projects_root
                or not resolved.is_relative_to(projects_root)
            ):
                raise RegistryError(
                    f"{self.adapter_config_path}: $.games.{game_id}.projectDir: "
                    "must resolve beneath PROJECTS_ROOT"
                )
            self.project_paths[game_id] = self.projects_root / configured
            self.project_dirs[game_id] = resolved
            self.commands[game_id] = {}
            for action, specs in adapter["commands"].items():
                if not specs:
                    raise RegistryError(
                        f"{self.adapter_config_path}: $.games.{game_id}.commands."
                        f"{action}: command sequence must be non-empty"
                    )
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
                    resolved_specs.append((configured_script, resolved_script, timeout))
                self.commands[game_id][action] = resolved_specs

        self._profiles: dict[str, dict[str, Any]] = {}
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
            self._profiles[path.stem] = copy.deepcopy(profile)

        missing_adapters = sorted(set(self._profiles) - set(self._adapters))
        missing_profiles = sorted(set(self._adapters) - set(self._profiles))
        pair_errors = [
            *(f"missing adapter for profile '{game_id}'" for game_id in missing_adapters),
            *(f"missing profile for adapter '{game_id}'" for game_id in missing_profiles),
        ]
        if pair_errors:
            raise RegistryError(f"registry: {'; '.join(pair_errors)}")

        for game_id, profile in self._profiles.items():
            profile_path = self.profiles_dir / f"{game_id}.json"
            declared_actions = set(self.commands[game_id]) | {"ui.refresh"}
            if self._adapters[game_id].get("propertyTypes"):
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
                    property_types = self._adapters[game_id].get("propertyTypes", {})
                    if key not in property_types:
                        raise RegistryError(
                            f"{profile_path}: $.controls[{control_index}].binding.key: "
                            f"property key '{key}' is not declared by adapter '{game_id}'"
                        )
                    property_type = property_types[key]
                    kind = control["kind"]
                    if kind not in PROPERTY_CONTROL_KINDS.get(property_type, frozenset()):
                        raise RegistryError(
                            f"{profile_path}: $.controls[{control_index}].kind: "
                            f"control kind '{kind}' is incompatible with property type "
                            f"'{property_type}' for binding key '{key}'"
                        )
                backend_risk = ACTION_POLICIES[action][0]
                if RISK_LEVELS.index(control["risk"]) < RISK_LEVELS.index(backend_risk):
                    raise RegistryError(
                        f"{profile_path}: $.controls[{control_index}].risk: "
                        f"action '{action}' requires backend risk '{backend_risk}'"
                    )

    @property
    def adapters(self) -> dict[str, Any]:
        return copy.deepcopy(self._adapters)

    @property
    def profiles(self) -> dict[str, dict[str, Any]]:
        return copy.deepcopy(self._profiles)

    @property
    def game_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._profiles))

    def adapter(self, game_id: str) -> dict[str, Any]:
        try:
            return copy.deepcopy(self._adapters[game_id])
        except KeyError as exc:
            raise RegistryError(f"no adapter config for game: {game_id}") from exc

    def profile(self, game_id: str) -> dict[str, Any]:
        try:
            return copy.deepcopy(self._profiles[game_id])
        except KeyError as exc:
            raise RegistryError(f"unknown game profile: {game_id}") from exc

    @staticmethod
    def _reject_symlink_components(path: Path, *, description: str) -> None:
        absolute = Path(os.path.abspath(path))
        components = [absolute, *absolute.parents]
        for component in reversed(components):
            try:
                mode = component.lstat().st_mode
            except OSError as exc:
                raise RegistryError(f"approved {description} is unavailable") from exc
            if stat.S_ISLNK(mode):
                raise RegistryError(
                    f"approved {description} contains a symlink component: {component}"
                )

    def validated_project_path(self, game_id: str) -> Path:
        configured = self.project_paths[game_id]
        expected = self.project_dirs[game_id]
        self._reject_symlink_components(configured, description="project path")
        try:
            resolved_root = self.projects_root.resolve(strict=True)
            resolved = configured.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise RegistryError("approved project path is unavailable") from exc
        if (
            resolved_root != self._resolved_projects_root
            or resolved != expected
            or resolved == resolved_root
            or not resolved.is_relative_to(resolved_root)
        ):
            raise RegistryError("approved project path no longer resolves beneath PROJECTS_ROOT")
        return resolved

    def validated_file_path(
        self,
        game_id: str,
        relative_path: Path,
        *,
        description: str,
        executable: bool = False,
    ) -> Path:
        project_dir = self.validated_project_path(game_id)
        configured = self.project_paths[game_id] / relative_path
        self._reject_symlink_components(configured, description=description)
        try:
            resolved = configured.resolve(strict=True)
            mode = configured.lstat().st_mode
        except (OSError, RuntimeError) as exc:
            raise RegistryError(f"approved {description} is unavailable") from exc
        if not resolved.is_relative_to(project_dir):
            raise RegistryError(
                f"approved {description} no longer resolves beneath game directory"
            )
        if not stat.S_ISREG(mode) or (executable and not mode & 0o111):
            requirement = "regular executable file" if executable else "regular file"
            raise RegistryError(f"approved {description} is no longer a {requirement}")
        return resolved

    def validated_commands(
        self, game_id: str, action: str
    ) -> list[tuple[list[str], Path, int]]:
        specs = self.commands[game_id].get(action)
        if not specs:
            raise RegistryError(f"action is not approved for {game_id}: {action}")
        project_dir = self.validated_project_path(game_id)
        validated: list[tuple[list[str], Path, int]] = []
        for configured_script, expected_script, timeout in specs:
            configured_path = self.project_paths[game_id] / configured_script
            try:
                mode = configured_path.lstat().st_mode
            except OSError:
                mode = 0
            if (
                configured_path.is_symlink()
                or not stat.S_ISREG(mode)
                or not mode & 0o111
            ):
                raise RegistryError(
                    "approved action script is no longer a regular executable "
                    f"non-symlink file: {configured_path.name}"
                )
            script = self.validated_file_path(
                game_id,
                configured_script,
                description="action script path",
                executable=True,
            )
            if script != expected_script:
                raise RegistryError(
                    "approved action script path changed after registry validation"
                )
            validated.append(([str(script)], project_dir, timeout))
        return validated


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
            raise RegistryError(
                f"{path}: {_json_path(error.absolute_path)}: {error.message}"
            )
