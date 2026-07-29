from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


class ControlEngineError(ValueError):
    """Raised when a game control profile or request is invalid."""


class ControlEngine:
    """Render game-specific controls from declarative, non-executable profiles."""

    _CONTROL_KINDS = {"button", "switch", "slider", "select", "text", "number", "readonly"}
    _PROFILE_FIELDS = {"schemaVersion", "id", "name", "description", "icon", "controls"}
    _CONTROL_FIELDS = {
        "id",
        "kind",
        "label",
        "group",
        "help",
        "risk",
        "restartRequired",
        "enabledWhen",
        "variant",
        "min",
        "max",
        "step",
        "unit",
        "maxLength",
        "value",
        "options",
        "binding",
    }
    _BINDING_FIELDS = {"action", "key"}
    _RISKS = {"read-only", "safe", "safe-mutation", "configuration", "service", "disruptive"}
    _BINDING_ACTIONS = {
        "property.set",
        "service.start",
        "service.stop",
        "service.restart",
        "backup.create",
        "ui.refresh",
    }
    def __init__(
        self,
        *,
        projects_root: Path,
        profiles_dir: Path,
        audit_path: Path,
        adapter_config_path: Path | None = None,
        command_runner: Callable[..., dict[str, Any]] | None = None,
    ):
        self.projects_root = Path(projects_root)
        self.profiles_dir = Path(profiles_dir)
        self.audit_path = Path(audit_path)
        self._adapter_config_path = (
            Path(adapter_config_path) if adapter_config_path
            else self.profiles_dir.parent / "game_adapters.json"
        )
        self._adapters: dict[str, Any] = {}
        self._load_adapters()
        self._plans: dict[str, dict[str, Any]] = {}
        self._plans_lock = threading.Lock()
        self._game_locks: dict[str, threading.Lock] = {}
        self._command_runner = command_runner or self._default_command_runner

    def _load_adapters(self) -> None:
        """Load game adapter config from the data-driven JSON file."""
        if not self._adapter_config_path.is_file():
            self._adapters = {}
            return
        data = json.loads(self._adapter_config_path.read_text(encoding="utf-8"))
        self._adapters = data.get("games", {})

    def _adapter_for(self, game_id: str) -> dict[str, Any]:
        adapter = self._adapters.get(game_id)
        if not adapter:
            raise ControlEngineError(f"no adapter config for game: {game_id}")
        return adapter

    def _property_types_for(self, game_id: str) -> dict[str, str]:
        return self._adapter_for(game_id).get("propertyTypes", {})

    def catalog(self) -> dict[str, Any]:
        games = [
            self.game_view(path.stem)
            for path in sorted(self.profiles_dir.glob("*.json"))
            if not path.name.startswith("_")
        ]
        return {
            "schemaVersion": "1.0",
            "generatedAt": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "allowedControlKinds": sorted(self._CONTROL_KINDS),
            "games": games,
        }

    def game_view(self, game_id: str) -> dict[str, Any]:
        profile = self._load_profile(game_id)
        profile_digest = hashlib.sha256(
            json.dumps(profile, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        properties = self._read_properties(game_id)
        controls: list[dict[str, Any]] = []
        for raw in profile.get("controls", []):
            self._validate_control(game_id, raw)
            control = dict(raw)
            binding = control.get("binding") or {}
            if binding.get("action") == "property.set":
                key = str(binding.get("key", ""))
                control["value"] = self._coerce_property(game_id, key, properties.get(key))
            controls.append(control)
        return {
            "schemaVersion": profile["schemaVersion"],
            "profileDigest": profile_digest,
            "id": profile["id"],
            "name": profile["name"],
            "description": profile.get("description", ""),
            "icon": profile.get("icon"),
            "controls": controls,
        }

    def plan(self, *, game_id: str, control_id: str, value: Any, actor: str) -> dict[str, Any]:
        view = self.game_view(game_id)
        control = next((item for item in view["controls"] if item["id"] == control_id), None)
        if control is None:
            raise ControlEngineError(f"unknown control: {game_id}.{control_id}")
        proposed = self._validate_value(control, value)
        action = control["binding"]["action"]
        plan_id = secrets.token_urlsafe(24)
        plan = {
            "ok": True,
            "planId": plan_id,
            "gameId": game_id,
            "gameName": view["name"],
            "controlId": control_id,
            "controlLabel": control.get("label", control_id),
            "action": action,
            "currentValue": control.get("value"),
            "proposedValue": proposed,
            "risk": control.get("risk", "safe"),
            "restartRequired": bool(control.get("restartRequired", False)),
            "requiresConfirmation": action != "ui.refresh",
            "actor": actor or "unknown",
            "expiresAt": time.time() + 300,
            "profileDigest": view["profileDigest"],
            "binding": dict(control["binding"]),
        }
        digest_payload = json.dumps(plan, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        plan["planDigest"] = hashlib.sha256(digest_payload).hexdigest()
        with self._plans_lock:
            self._plans[plan_id] = plan
        return {key: value for key, value in plan.items() if key != "binding"}

    def apply(
        self,
        *,
        plan_id: str,
        actor: str,
        confirmed: bool,
        plan_digest: str | None = None,
    ) -> dict[str, Any]:
        with self._plans_lock:
            plan = self._plans.get(plan_id)
            if plan is None:
                raise ControlEngineError("unknown or already used plan")
            if not confirmed:
                raise ControlEngineError("confirmation required")
            if (actor or "unknown") != plan["actor"]:
                raise ControlEngineError("actor does not match the plan actor")
            if plan_digest is not None and plan_digest != plan.get("planDigest"):
                raise ControlEngineError("digest does not match")
            if time.time() > float(plan["expiresAt"]):
                self._plans.pop(plan_id, None)
                raise ControlEngineError("plan expired")
            # One-time use: consume before mutation so a second apply cannot race.
            self._plans.pop(plan_id, None)

        game_id = str(plan["gameId"])
        lock = self._game_locks.setdefault(game_id, threading.Lock())
        with lock:
            action = plan["action"]
            rollback_path: Path | None = None
            output = ""
            if action == "property.set":
                rollback_path = self._apply_property_change(plan)
            else:
                command_result = self._apply_script_action(plan)
                output = str(command_result.get("output", ""))
                if not command_result.get("ok"):
                    raise ControlEngineError(output or f"action failed: {action}")
            record = {
                "timestamp": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                "actor": actor or "unknown",
                "plannedBy": plan["actor"],
                "planId": plan_id,
                "planDigest": plan.get("planDigest"),
                "gameId": plan["gameId"],
                "controlId": plan["controlId"],
                "action": action,
                "before": plan["currentValue"],
                "after": plan["proposedValue"],
                "restartRequired": plan["restartRequired"],
                "rollbackPath": str(rollback_path) if rollback_path else None,
                "output": output,
            }
            self._append_audit(record)
            return {"ok": True, **record}

    def _apply_property_change(self, plan: dict[str, Any]) -> Path:
        game_id = str(plan["gameId"])
        adapter = self._adapter_for(game_id)
        property_types = adapter.get("propertyTypes", {})
        if not property_types:
            raise ControlEngineError(f"property writes are not supported for {game_id}")
        binding = plan["binding"]
        key = str(binding.get("key", ""))
        if key not in property_types:
            raise ControlEngineError(f"unsupported property binding: {game_id}.{key}")
        project_dir = self.projects_root / adapter["projectDir"]
        path = project_dir / "server.properties"
        if not path.is_file():
            raise ControlEngineError(f"{game_id} server.properties was not found")
        current = self._coerce_property(game_id, key, self._read_properties(game_id).get(key))
        if current != plan["currentValue"]:
            raise ControlEngineError("property changed after preview; create a new plan")

        rollback_dir = path.parent / "control-backups"
        rollback_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        rollback_path = rollback_dir / f"server.properties.{stamp}.bak"
        shutil.copy2(path, rollback_path)

        proposed = self._serialize_property(plan["proposedValue"])
        original = path.read_text(encoding="utf-8", errors="strict")
        output: list[str] = []
        replaced = False
        for line in original.splitlines(keepends=True):
            if not line.lstrip().startswith("#") and line.partition("=")[0] == key:
                ending = "\n" if line.endswith("\n") else ""
                output.append(f"{key}={proposed}{ending}")
                replaced = True
            else:
                output.append(line)
        if not replaced:
            if output and not output[-1].endswith("\n"):
                output[-1] += "\n"
            output.append(f"{key}={proposed}\n")
        temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            temp.write_text("".join(output), encoding="utf-8")
            os.chmod(temp, path.stat().st_mode)
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
        return rollback_path

    def _apply_script_action(self, plan: dict[str, Any]) -> dict[str, Any]:
        commands = self._commands_for(str(plan["gameId"]), str(plan["action"]))
        outputs: list[str] = []
        for argv, cwd, timeout in commands:
            script = Path(argv[0])
            if not script.is_file():
                raise ControlEngineError(f"approved action script is missing: {script.name}")
            result = self._command_runner(argv, cwd=cwd, timeout=timeout)
            outputs.append(str(result.get("output", "")))
            if not result.get("ok"):
                return {
                    "ok": False,
                    "exitCode": result.get("exitCode"),
                    "output": "\n".join(item for item in outputs if item)[-4000:],
                }
        return {"ok": True, "exitCode": 0, "output": "\n".join(item for item in outputs if item)[-4000:]}

    def _commands_for(self, game_id: str, action: str) -> list[tuple[list[str], Path, int]]:
        adapter = self._adapter_for(game_id)
        commands = adapter.get("commands", {})
        specs = commands.get(action)
        if not specs:
            raise ControlEngineError(f"action is not approved for {game_id}: {action}")
        project_dir = self.projects_root / adapter["projectDir"]
        return [([str(project_dir / script)], project_dir, timeout) for script, timeout in specs]

    @staticmethod
    def _default_command_runner(argv: list[str], *, cwd: Path, timeout: int) -> dict[str, Any]:
        try:
            completed = subprocess.run(
                argv,
                cwd=cwd,
                timeout=timeout,
                check=False,
                capture_output=True,
                text=True,
                env={**os.environ, "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            )
        except subprocess.TimeoutExpired as exc:
            output = "\n".join(filter(None, [str(exc.stdout or ""), str(exc.stderr or "")]))
            return {"ok": False, "exitCode": None, "output": (output or "action timed out")[-4000:]}
        output = "\n".join(filter(None, [completed.stdout.strip(), completed.stderr.strip()]))
        return {"ok": completed.returncode == 0, "exitCode": completed.returncode, "output": output[-4000:]}

    @staticmethod
    def _serialize_property(value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    def _append_audit(self, record: dict[str, Any]) -> None:
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _validate_value(self, control: dict[str, Any], value: Any) -> Any:
        kind = control["kind"]
        if kind in {"slider", "number"}:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ControlEngineError(f"{control['id']} requires a number")
            minimum = control.get("min")
            maximum = control.get("max")
            if minimum is not None and value < minimum:
                raise ControlEngineError(f"{control['id']} is below minimum {minimum}")
            if maximum is not None and value > maximum:
                raise ControlEngineError(f"{control['id']} is above maximum {maximum}")
            step = control.get("step")
            if step == 1:
                return int(value)
            return value
        if kind == "switch":
            if not isinstance(value, bool):
                raise ControlEngineError(f"{control['id']} requires true or false")
            return value
        if kind == "select":
            options = {item.get("value") for item in control.get("options", []) if isinstance(item, dict)}
            if value not in options:
                raise ControlEngineError(f"invalid option for {control['id']}")
            return value
        if kind == "text":
            if not isinstance(value, str):
                raise ControlEngineError(f"{control['id']} requires text")
            maximum = int(control.get("maxLength", 200))
            if len(value) > maximum or "\n" in value or "\r" in value:
                raise ControlEngineError(f"invalid text for {control['id']}")
            return value
        if kind == "button":
            return None
        return value

    def _validate_control(self, game_id: str, control: Any) -> None:
        if not isinstance(control, dict):
            raise ControlEngineError(f"invalid control in profile: {game_id}")
        unknown = sorted(set(control) - self._CONTROL_FIELDS)
        if unknown:
            raise ControlEngineError(f"unknown control fields: {', '.join(unknown)}")
        kind = control.get("kind")
        if kind not in self._CONTROL_KINDS:
            raise ControlEngineError(f"unsupported control kind: {kind}")
        control_id = control.get("id")
        if not isinstance(control_id, str) or not control_id:
            raise ControlEngineError("control id is required")
        risk = control.get("risk")
        if risk is not None and risk not in self._RISKS:
            raise ControlEngineError(f"unsupported control risk: {risk}")
        binding = control.get("binding")
        if not isinstance(binding, dict):
            raise ControlEngineError(f"control binding is required: {control_id}")
        unknown_binding = sorted(set(binding) - self._BINDING_FIELDS)
        if unknown_binding:
            raise ControlEngineError(
                f"unknown binding fields: {', '.join(unknown_binding)}"
            )
        action = binding.get("action")
        if action not in self._BINDING_ACTIONS:
            raise ControlEngineError(f"unsupported control action: {action}")
        if any(key in binding for key in ("command", "shell", "script", "argv")):
            raise ControlEngineError("executable commands are not allowed in control profiles")
        if action == "property.set" and not binding.get("key"):
            raise ControlEngineError(f"property binding requires key: {control_id}")
        if action != "property.set" and binding.get("key"):
            raise ControlEngineError(f"key is only valid for property.set: {control_id}")

    def _load_profile(self, game_id: str) -> dict[str, Any]:
        if not game_id or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-" for ch in game_id):
            raise ControlEngineError("invalid game id")
        path = self.profiles_dir / f"{game_id}.json"
        if not path.is_file():
            raise ControlEngineError(f"unknown game profile: {game_id}")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ControlEngineError(f"invalid game profile: {game_id}")
        unknown = sorted(set(data) - self._PROFILE_FIELDS)
        if unknown:
            raise ControlEngineError(f"unknown profile fields: {', '.join(unknown)}")
        schema = data.get("schemaVersion")
        if schema != "1.0":
            raise ControlEngineError(f"unsupported profile schema: {schema}")
        if data.get("id") != game_id or not data.get("name"):
            raise ControlEngineError(f"invalid game profile: {game_id}")
        if not isinstance(data.get("controls"), list):
            raise ControlEngineError(f"invalid controls list: {game_id}")
        return data

    def _read_properties(self, game_id: str) -> dict[str, str]:
        adapter = self._adapters.get(game_id)
        if not adapter or not adapter.get("propertyTypes"):
            return {}
        path = self.projects_root / adapter["projectDir"] / "server.properties"
        if not path.is_file():
            return {}
        values: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line or line.lstrip().startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key] = value
        return values

    def _coerce_property(self, game_id: str, key: str, value: str | None) -> Any:
        property_types = self._property_types_for(game_id)
        if key not in property_types:
            raise ControlEngineError(f"unsupported property binding: {game_id}.{key}")
        if value is None:
            return None
        kind = property_types[key]
        if kind == "integer":
            return int(value)
        if kind == "boolean":
            return value.strip().lower() == "true"
        return value
