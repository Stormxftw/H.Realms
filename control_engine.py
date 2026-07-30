from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import stat
import subprocess
import threading
import time
from concurrent.futures import Executor, ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from operations import OperationStore
from registry import ACTION_POLICIES, RISK_LEVELS, GameRegistry, RegistryError
from restart_state import RestartStateStore, pending_restart_presentation


class ControlEngineError(ValueError):
    """Raised when a game control profile or request is invalid."""


class OperationConflictError(ControlEngineError):
    """Raised when a game already has an active mutation."""

    def __init__(self, active_operation_id: str, operation: dict[str, Any] | None = None):
        self.active_operation_id = active_operation_id
        self.operation = operation
        super().__init__(f"another mutation is active for this game: {active_operation_id}")


class OperationRejectedError(ControlEngineError):
    """Raised when an identifiable apply request is rejected durably."""

    def __init__(self, message: str, operation: dict[str, Any]):
        self.operation = operation
        super().__init__(message)


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
    _RISKS = set(RISK_LEVELS)
    _BINDING_ACTIONS = set(ACTION_POLICIES)
    _CAPABILITY_ACTIONS = {
        "canStart": "service.start",
        "canStop": "service.stop",
        "canRestart": "service.restart",
        "canConfigure": "property.set",
        "canBackup": "backup.create",
    }
    _ACTION_CAPABILITIES = {
        action: capability for capability, action in _CAPABILITY_ACTIONS.items()
    }
    def __init__(
        self,
        *,
        projects_root: Path,
        profiles_dir: Path,
        audit_path: Path,
        adapter_config_path: Path | None = None,
        command_runner: Callable[..., dict[str, Any]] | None = None,
        operation_store: OperationStore | None = None,
        restart_state_store: RestartStateStore | None = None,
        status_provider: Callable[[str], dict[str, Any]] | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
        executor: Executor | None = None,
        executor_workers: int = 4,
        plan_ttl_seconds: int = 300,
        max_pending_plans: int = 256,
        postcondition_timeout: float = 30.0,
        poll_interval: float = 1.0,
    ):
        self.projects_root = Path(projects_root)
        self.profiles_dir = Path(profiles_dir)
        self.audit_path = Path(audit_path)
        self._adapter_config_path = (
            Path(adapter_config_path) if adapter_config_path
            else self.profiles_dir.parent / "game_adapters.json"
        )
        try:
            self._registry = GameRegistry(
                projects_root=self.projects_root,
                profiles_dir=self.profiles_dir,
                adapter_config_path=self._adapter_config_path,
            )
        except RegistryError as exc:
            raise ControlEngineError(str(exc)) from exc
        self._plans: dict[str, dict[str, Any]] = {}
        self._plans_lock = threading.Lock()
        self._mutation_lock = threading.Lock()
        self._game_locks: dict[str, threading.Lock] = {}
        self._active_by_game: dict[str, str] = {}
        self._audit_lock = threading.Lock()
        self._command_runner = command_runner or self._default_command_runner
        self._operation_store = operation_store or OperationStore(
            db_path=self.audit_path.with_name("operations.db"),
            private_path_prefixes=(Path.home(), self.projects_root),
        )
        self._restart_state_store = restart_state_store or RestartStateStore(
            self.audit_path.with_name("restart-state.json")
        )
        self._status_provider = status_provider or self._unknown_status
        self._status_checks_enabled = status_provider is not None
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic or time.monotonic
        self._sleeper = sleeper or time.sleep
        if executor_workers < 1 or executor_workers > 32:
            raise ValueError("executor_workers must be from 1 through 32")
        if plan_ttl_seconds < 1:
            raise ValueError("plan_ttl_seconds must be positive")
        if max_pending_plans < 1:
            raise ValueError("max_pending_plans must be positive")
        if postcondition_timeout <= 0 or poll_interval <= 0:
            raise ValueError("postcondition timeout and interval must be positive")
        self._plan_ttl_seconds = plan_ttl_seconds
        self._max_pending_plans = max_pending_plans
        self._postcondition_timeout = float(postcondition_timeout)
        self._poll_interval = float(poll_interval)
        self._executor = executor or ThreadPoolExecutor(
            max_workers=executor_workers,
            thread_name_prefix="game-host-operation",
        )
        self._owns_executor = executor is None
        for game_id in self._registry.game_ids:
            for control in self._registry.profile(game_id).get("controls", []):
                self._validate_control(game_id, control)

    @staticmethod
    def _unknown_status(game_id: str) -> dict[str, Any]:
        return {
            "id": game_id,
            "state": "unknown",
            "online": False,
            "process": {
                "ok": False,
                "running": None,
                "pid": None,
                "error": "no status provider configured",
            },
        }

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def close(self) -> None:
        if self._owns_executor:
            self._executor.shutdown(wait=True)


    def _adapter_for(self, game_id: str) -> dict[str, Any]:
        try:
            return self._registry.adapter(game_id)
        except RegistryError as exc:
            raise ControlEngineError(str(exc)) from exc

    def _property_types_for(self, game_id: str) -> dict[str, str]:
        return self._adapter_for(game_id).get("propertyTypes", {})

    def catalog(self) -> dict[str, Any]:
        games = [
            self.game_view(game_id)
            for game_id in self._registry.game_ids
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
        readiness = self.readiness(game_id)
        controls: list[dict[str, Any]] = []
        for raw in profile.get("controls", []):
            self._validate_control(game_id, raw)
            control = dict(raw)
            binding = control.get("binding") or {}
            if binding.get("action") == "property.set":
                key = str(binding.get("key", ""))
                control["value"] = self._coerce_property(game_id, key, properties.get(key))
            action_name = binding.get("action")
            capability = self._ACTION_CAPABILITIES.get(
                action_name if isinstance(action_name, str) else ""
            )
            if capability and not readiness["capabilities"][capability]:
                control["disabled"] = True
                control["disabledReason"] = readiness["capabilityReasons"][capability]
            controls.append(control)
        return {
            "schemaVersion": profile["schemaVersion"],
            "profileDigest": profile_digest,
            "id": profile["id"],
            "name": profile["name"],
            "description": profile.get("description", ""),
            "icon": profile.get("icon"),
            **readiness,
            "controls": controls,
        }

    def readiness(self, game_id: str) -> dict[str, Any]:
        """Return filesystem-backed installation readiness and mutation capabilities."""
        adapter = self._adapter_for(game_id)
        project_path = self._registry.project_paths[game_id]
        capabilities = {
            capability: False
            for capability in (
                *self._CAPABILITY_ACTIONS,
                "canRestore",
                "canViewLogs",
            )
        }
        reasons: dict[str, str] = {}
        blockers: list[dict[str, str]] = []

        if not project_path.exists():
            message = "Create the configured project directory and install the game server files."
            blockers.append(
                {
                    "code": "project_missing",
                    "kind": "project",
                    "message": message,
                }
            )
            for capability in capabilities:
                reasons[capability] = message
            return {
                "readiness": "needs_setup",
                "projectPresent": False,
                "blockers": blockers,
                "capabilities": capabilities,
                "capabilityReasons": reasons,
            }

        if not project_path.is_dir() or project_path.is_symlink():
            message = "Fix the configured project directory; it must be a real directory."
            blockers.append(
                {
                    "code": "project_unavailable",
                    "kind": "project",
                    "message": message,
                }
            )
            for capability in capabilities:
                reasons[capability] = message
            return {
                "readiness": "unavailable",
                "projectPresent": True,
                "blockers": blockers,
                "capabilities": capabilities,
                "capabilityReasons": reasons,
            }

        setup_blockers: list[dict[str, str]] = []
        invalid_evidence = False
        try:
            resolved_project = project_path.resolve(strict=True)
        except (OSError, RuntimeError):
            resolved_project = project_path
        for evidence in adapter.get("requiredPaths", []):
            relative = Path(evidence["path"])
            kind = str(evidence["kind"])
            expected_type = str(evidence["type"])
            candidate = project_path / relative
            try:
                resolved = candidate.resolve(strict=True)
            except (OSError, RuntimeError):
                resolved = None
            if resolved is None:
                setup_blockers.append(
                    {
                        "code": f"{kind}_missing",
                        "kind": kind,
                        "path": relative.as_posix(),
                        "message": evidence.get("description")
                        or f"Add the required {kind}: {relative.as_posix()}.",
                    }
                )
                continue
            valid_type = resolved.is_file() if expected_type == "file" else resolved.is_dir()
            if (
                not resolved.is_relative_to(resolved_project)
                or candidate.is_symlink()
                or not valid_type
            ):
                invalid_evidence = True
                setup_blockers.append(
                    {
                        "code": f"{kind}_invalid",
                        "kind": kind,
                        "path": relative.as_posix(),
                        "message": f"Fix required {kind} evidence: {relative.as_posix()}.",
                    }
                )
        if setup_blockers:
            blockers.extend(setup_blockers)
            message = "Complete the listed installation setup before running mutations."
            for capability in capabilities:
                reasons[capability] = message
            return {
                "readiness": "misconfigured" if invalid_evidence else "needs_setup",
                "projectPresent": True,
                "blockers": blockers,
                "capabilities": capabilities,
                "capabilityReasons": reasons,
            }

        for capability, action in self._CAPABILITY_ACTIONS.items():
            try:
                if action == "property.set":
                    if not adapter.get("propertyTypes"):
                        raise RegistryError("no configurable properties are declared")
                    self._registry.validated_file_path(
                        game_id,
                        Path("server.properties"),
                        description="server configuration",
                    )
                else:
                    self._registry.validated_commands(game_id, action)
            except RegistryError as exc:
                reason = f"Configure {action} for {game_id}: {exc}."
                reasons[capability] = reason
                if action in adapter.get("commands", {}) or action == "property.set":
                    blockers.append(
                        {
                            "code": "capability_misconfigured",
                            "kind": "script" if action != "property.set" else "config",
                            "capability": capability,
                            "message": reason,
                        }
                    )
            else:
                capabilities[capability] = True

        reasons["canRestore"] = "Restore is not implemented for this game adapter."
        reasons["canViewLogs"] = "Log viewing is not implemented in this v1 slice."
        readiness = "misconfigured" if blockers else "ready"
        return {
            "readiness": readiness,
            "projectPresent": True,
            "blockers": blockers,
            "capabilities": capabilities,
            "capabilityReasons": reasons,
        }

    def plan(self, *, game_id: str, control_id: str, value: Any, actor: str) -> dict[str, Any]:
        view = self.game_view(game_id)
        control = next((item for item in view["controls"] if item["id"] == control_id), None)
        if control is None:
            raise ControlEngineError(f"unknown control: {game_id}.{control_id}")
        action = control["binding"]["action"]
        if action == "property.set":
            key = str(control["binding"].get("key", ""))
            if self._property_types_for(game_id).get(key) == "integer" and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or (isinstance(value, float) and not value.is_integer())
            ):
                raise ControlEngineError(f"{control['id']} requires a whole number")
        proposed = self._validate_value(control, value)
        self._ensure_action_ready(game_id, action)
        _backend_risk, requires_confirmation = ACTION_POLICIES[action]
        plan_id = secrets.token_urlsafe(24)
        now = self._now().timestamp()
        planned_status = self._status_provider(game_id)
        status_fingerprint = self._status_fingerprint(planned_status)
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
            "requiresConfirmation": requires_confirmation,
            "actor": actor or "unknown",
            "expiresAt": now + self._plan_ttl_seconds,
            "profileDigest": view["profileDigest"],
            "statusFingerprint": status_fingerprint,
            "binding": dict(control["binding"]),
            "createdAt": now,
            "plannedStatus": planned_status,
        }
        digest_payload = json.dumps(plan, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        plan["planDigest"] = hashlib.sha256(digest_payload).hexdigest()
        with self._plans_lock:
            self._purge_plans_locked(now)
            while len(self._plans) >= self._max_pending_plans:
                # dict iteration is insertion ordered, so equal timestamps evict
                # the genuinely oldest preview rather than a random token order.
                oldest_id = min(
                    self._plans,
                    key=lambda item: float(self._plans[item].get("createdAt", 0)),
                )
                self._plans.pop(oldest_id, None)
            self._plans[plan_id] = plan
        return {
            key: value
            for key, value in plan.items()
            if key not in {"binding", "createdAt", "plannedStatus"}
        }

    def _purge_plans_locked(self, now: float | None = None) -> None:
        current = self._now().timestamp() if now is None else now
        for plan_id, plan in list(self._plans.items()):
            if current > float(plan["expiresAt"]):
                self._plans.pop(plan_id, None)

    @staticmethod
    def _status_fingerprint(status: dict[str, Any]) -> str:
        process = status.get("process") if isinstance(status, dict) else None
        process = process if isinstance(process, dict) else {}
        stable = {
            "state": status.get("state") if isinstance(status, dict) else None,
            "process": {
                "ok": process.get("ok"),
                "running": process.get("running"),
                "pid": process.get("pid"),
                "error": process.get("error"),
                "detector": process.get("detector"),
            },
        }
        payload = json.dumps(stable, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _ensure_action_ready(self, game_id: str, action: str) -> None:
        if action == "ui.refresh":
            return
        capability = self._ACTION_CAPABILITIES.get(action)
        if capability:
            readiness = self.readiness(game_id)
            if not readiness["capabilities"][capability]:
                reason = readiness["capabilityReasons"].get(
                    capability, "required installation evidence is unavailable"
                )
                raise ControlEngineError(
                    f"action unavailable for {game_id}: {reason}"
                )
        try:
            if action == "property.set":
                self._registry.validated_file_path(
                    game_id,
                    Path("server.properties"),
                    description="property path",
                )
            else:
                self._registry.validated_commands(game_id, action)
        except RegistryError as exc:
            raise ControlEngineError(f"action unavailable for {game_id}: {exc}") from exc

    def apply(
        self,
        *,
        plan_id: str,
        actor: str,
        confirmed: bool,
        plan_digest: str | None = None,
        source: str = "unknown",
    ) -> dict[str, Any]:
        plan = self._consume_plan(
            plan_id=plan_id,
            actor=actor,
            confirmed=confirmed,
            plan_digest=plan_digest,
        )
        operation = self._queue_operation(plan, source=source)
        result = self._execute_operation(operation["operationId"], plan)
        if result["state"] == "failed":
            raise OperationRejectedError(
                result.get("output") or f"action failed: {plan['action']}",
                result,
            )
        return result

    def submit_apply(
        self,
        *,
        plan_id: str,
        actor: str,
        confirmed: bool,
        plan_digest: str | None = None,
        source: str = "unknown",
    ) -> dict[str, Any]:
        """Queue one mutation and return its durable operation record promptly."""
        plan = self._consume_plan(
            plan_id=plan_id,
            actor=actor,
            confirmed=confirmed,
            plan_digest=plan_digest,
        )
        operation = self._queue_operation(plan, source=source)
        operation_id = operation["operationId"]
        try:
            self._executor.submit(self._execute_operation, operation_id, plan)
        except Exception as exc:
            self._release_game(str(plan["gameId"]), operation_id)
            failed = self._operation_store.transition(
                operation_id,
                "failed",
                output=f"background executor rejected operation: {exc}",
                postcondition={"verified": False, "idempotent": False},
                recovery_note="no command was launched; retry with a fresh plan",
            )
            raise OperationRejectedError(str(exc), failed) from exc
        return operation

    def operation(self, operation_id: str) -> dict[str, Any] | None:
        return self._operation_store.get(operation_id)

    def operations(
        self,
        *,
        limit: int = 50,
        game_id: str | None = None,
        state: str | list[str] | None = None,
    ) -> list[dict[str, Any]]:
        return self._operation_store.list(limit=limit, game_id=game_id, state=state)

    def recover_interrupted_operations(self) -> int:
        return self._operation_store.recover_interrupted(
            "host process restarted while the operation was running; verify actual game state"
        )

    def pending_restart(self, game_id: str, status: str = "unknown") -> dict[str, Any]:
        pending = self._restart_state_store.list_pending(game_id)
        presentation_state = {
            "running_ready": "running",
            "running_degraded": "degraded",
            "stopped": "stopped",
        }.get(status, "unknown")
        return {
            **pending_restart_presentation(presentation_state, len(pending)),
            "items": pending,
        }

    def _consume_plan(
        self,
        *,
        plan_id: str,
        actor: str,
        confirmed: bool,
        plan_digest: str | None,
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
            if self._now().timestamp() > float(plan["expiresAt"]):
                self._plans.pop(plan_id, None)
                raise ControlEngineError("plan expired")
            self._plans.pop(plan_id, None)
            return plan

    def _game_lock_for(self, game_id: str) -> threading.Lock:
        with self._mutation_lock:
            return self._game_locks.setdefault(game_id, threading.Lock())

    def _queue_operation(self, plan: dict[str, Any], *, source: str) -> dict[str, Any]:
        game_id = str(plan["gameId"])
        with self._mutation_lock:
            active_operation_id = self._active_by_game.get(game_id)
            if active_operation_id is not None:
                raise OperationConflictError(
                    active_operation_id,
                    self._operation_store.get(active_operation_id),
                )
            operation = self._operation_store.create(
                game_id=game_id,
                action=str(plan["action"]),
                actor=str(plan["actor"]),
                source=source or "unknown",
                precondition={
                    "profileDigest": plan["profileDigest"],
                    "statusFingerprint": plan["statusFingerprint"],
                    "status": plan.get("plannedStatus"),
                },
            )
            self._active_by_game[game_id] = operation["operationId"]
        return operation

    def _release_game(self, game_id: str, operation_id: str) -> None:
        with self._mutation_lock:
            if self._active_by_game.get(game_id) == operation_id:
                self._active_by_game.pop(game_id, None)

    def _revalidate_plan(self, plan: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        current_digest = self.game_view(str(plan["gameId"]))["profileDigest"]
        if current_digest != plan["profileDigest"]:
            return {}, "profile changed after preview; create a new plan"
        status = self._status_provider(str(plan["gameId"]))
        if self._status_checks_enabled and (
            self._status_fingerprint(status) != plan["statusFingerprint"]
        ):
            return status, "status changed after preview; create a new plan"
        return status, None

    @staticmethod
    def _lifecycle_precondition(
        action: str, status: dict[str, Any]
    ) -> tuple[bool, bool, str | None]:
        process = status.get("process") if isinstance(status, dict) else None
        process = process if isinstance(process, dict) else {}
        state = status.get("state") if isinstance(status, dict) else None
        running = process.get("running")
        if process.get("ok") is not True or type(running) is not bool:
            return False, False, "unsafe lifecycle precondition: process state is unknown"
        if state not in {"running_ready", "stopped"}:
            return False, False, f"unsafe lifecycle precondition: server state is {state or 'unknown'}"
        if action == "service.start" and running:
            return True, True, None
        if action == "service.stop" and not running:
            return True, True, None
        if action == "service.restart" and not running:
            return False, False, "unsafe lifecycle precondition: cannot restart a stopped server"
        return True, False, None

    def _poll_lifecycle_postcondition(
        self, game_id: str, action: str
    ) -> tuple[bool, dict[str, Any]]:
        if not self._status_checks_enabled:
            return True, self._status_provider(game_id)
        expected_running = action != "service.stop"
        deadline = self._monotonic() + self._postcondition_timeout
        while True:
            observed = self._status_provider(game_id)
            process = observed.get("process") if isinstance(observed, dict) else None
            process = process if isinstance(process, dict) else {}
            expected_state = "running_ready" if expected_running else "stopped"
            if (
                process.get("ok") is True
                and process.get("running") is expected_running
                and observed.get("state") == expected_state
            ):
                return True, observed
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return False, observed
            self._sleeper(min(self._poll_interval, remaining))

    def _result_fields(
        self,
        plan: dict[str, Any],
        *,
        rollback_path: Path | None,
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "timestamp": self._now().astimezone().isoformat(timespec="seconds"),
            "plannedBy": plan["actor"],
            "planId": plan["planId"],
            "planDigest": plan.get("planDigest"),
            "controlId": plan["controlId"],
            "before": plan["currentValue"],
            "after": plan["proposedValue"],
            "restartRequired": plan["restartRequired"],
            "rollbackPath": str(rollback_path) if rollback_path else None,
        }

    def _execute_operation(
        self, operation_id: str, plan: dict[str, Any]
    ) -> dict[str, Any]:
        game_id = str(plan["gameId"])
        action = str(plan["action"])
        lock = self._game_lock_for(game_id)
        started = False
        command_result: dict[str, Any] = {}
        try:
            with lock:
                status, rejection = self._revalidate_plan(plan)
                idempotent = False
                if rejection is None and action.startswith("service.") and self._status_checks_enabled:
                    safe, idempotent, rejection = self._lifecycle_precondition(action, status)
                    if not safe and rejection is None:
                        rejection = "unsafe lifecycle precondition"
                if rejection is not None:
                    return self._operation_store.transition(
                        operation_id,
                        "failed",
                        output=rejection,
                        postcondition={
                            "verified": False,
                            "idempotent": False,
                            "observed": status,
                        },
                        recovery_note="no command was launched; refresh status and create a new plan",
                    )

                self._operation_store.transition(operation_id, "running")
                started = True
                rollback_path: Path | None = None
                output = ""
                if idempotent:
                    postcondition = {
                        "verified": True,
                        "idempotent": True,
                        "observed": status,
                    }
                elif action == "property.set":
                    rollback_path = self._apply_property_change(plan)
                    actual = self._read_properties(game_id).get(
                        str(plan["binding"].get("key", ""))
                    )
                    actual = self._coerce_property(
                        game_id, str(plan["binding"].get("key", "")), actual
                    )
                    if actual != plan["proposedValue"]:
                        raise ControlEngineError("configuration write postcondition was not verified")
                    postcondition = {
                        "verified": True,
                        "idempotent": False,
                        "configuredValue": actual,
                    }
                else:
                    command_result = self._apply_script_action(plan)
                    output = str(command_result.get("output", ""))
                    if not command_result.get("ok"):
                        raise ControlEngineError(output or f"action failed: {action}")
                    if action.startswith("service."):
                        verified, observed = self._poll_lifecycle_postcondition(game_id, action)
                        postcondition = {
                            "verified": verified,
                            "idempotent": False,
                            "observed": observed,
                        }
                        if not verified:
                            raise ControlEngineError(
                                "action exited successfully but the lifecycle postcondition was not verified"
                            )
                    else:
                        postcondition = {
                            "verified": True,
                            "idempotent": False,
                            "exitCode": command_result.get("exitCode"),
                        }

                result_fields = self._result_fields(plan, rollback_path=rollback_path)
                postcondition["result"] = result_fields
                transition_time = self._now().isoformat()
                if action == "property.set" and plan["restartRequired"]:
                    self._restart_state_store.record_change(
                        game_id=game_id,
                        control_id=str(plan["controlId"]),
                        configured_value=plan["proposedValue"],
                        effective_value=plan["currentValue"],
                        originating_operation_id=operation_id,
                        changed_at=transition_time,
                    )
                elif action in {"service.start", "service.restart"}:
                    self._restart_state_store.mark_verified_transition(
                        game_id, transition_time, successful=True
                    )

                audit_record = {
                    **result_fields,
                    "actor": plan["actor"],
                    "gameId": game_id,
                    "action": action,
                    "output": output,
                }
                self._append_audit(audit_record)
                completed = self._operation_store.transition(
                    operation_id,
                    "succeeded",
                    output=output,
                    postcondition=postcondition,
                )
                # The durable store intentionally redacts private absolute paths.
                # Preserve the historical synchronous return contract only for
                # the in-process caller that just created this rollback file.
                if rollback_path is not None:
                    completed["rollbackPath"] = str(rollback_path)
                return completed
        except Exception as exc:
            recovery_note = "mutation may have changed host state; inspect status before retrying"
            if (
                action == "service.restart"
                and int(command_result.get("completedSteps", 0)) > 0
            ):
                recovery_note = (
                    "restart partially completed after stop; the server may be stopped; "
                    "verify status before starting again"
                )
            current = self._operation_store.get(operation_id)
            if current is not None and current["state"] in {"queued", "running"}:
                return self._operation_store.transition(
                    operation_id,
                    "failed",
                    output=str(exc),
                    postcondition={"verified": False, "idempotent": False},
                    recovery_note=recovery_note,
                )
            raise
        finally:
            self._release_game(game_id, operation_id)

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
        try:
            path = self._registry.validated_file_path(
                game_id,
                Path("server.properties"),
                description="property path",
            )
        except RegistryError as exc:
            raise ControlEngineError(str(exc)) from exc
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
        completed_steps = 0
        for step, (argv, cwd, timeout) in enumerate(commands, start=1):
            script = Path(argv[0])
            try:
                mode = script.lstat().st_mode
            except OSError:
                mode = 0
            if script.is_symlink() or not stat.S_ISREG(mode) or not mode & 0o111:
                raise ControlEngineError(
                    "approved action script is no longer a regular executable "
                    f"non-symlink file: {script.name}"
                )
            result = self._command_runner(argv, cwd=cwd, timeout=timeout)
            outputs.append(str(result.get("output", "")))
            if not result.get("ok"):
                return {
                    "ok": False,
                    "exitCode": result.get("exitCode"),
                    "output": "\n".join(item for item in outputs if item)[-4000:],
                    "completedSteps": completed_steps,
                    "failedStep": step,
                }
            completed_steps += 1
        return {
            "ok": True,
            "exitCode": 0,
            "output": "\n".join(item for item in outputs if item)[-4000:],
            "completedSteps": completed_steps,
        }

    def _commands_for(self, game_id: str, action: str) -> list[tuple[list[str], Path, int]]:
        self._adapter_for(game_id)
        try:
            return self._registry.validated_commands(game_id, action)
        except RegistryError as exc:
            raise ControlEngineError(str(exc)) from exc

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
        with self._audit_lock:
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
        if isinstance(action, str) and (
            action.startswith("service.") or action == "backup.create"
        ) and kind != "button":
            raise ControlEngineError(
                f"lifecycle and backup actions require a button control: {control_id}"
            )
        if kind == "select" and not control.get("options"):
            raise ControlEngineError(
                f"select control requires at least one option: {control_id}"
            )

    def _load_profile(self, game_id: str) -> dict[str, Any]:
        if not game_id or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-" for ch in game_id):
            raise ControlEngineError("invalid game id")
        try:
            return self._registry.profile(game_id)
        except RegistryError as exc:
            raise ControlEngineError(str(exc)) from exc

    def _read_properties(self, game_id: str) -> dict[str, str]:
        adapter = self._adapter_for(game_id)
        if not adapter.get("propertyTypes"):
            return {}
        try:
            path = self._registry.validated_file_path(
                game_id,
                Path("server.properties"),
                description="property path",
            )
        except RegistryError:
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
