"""OpenClaw gateway status, authentication, and lifecycle operations."""

from __future__ import annotations

import os
import re
from time import perf_counter, sleep
from typing import Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

from .local import CommandError


class GatewayManagementMixin:
    """Manage gateway health, auth, process state, and agent discovery."""

    def check_server_status(self) -> Dict[str, object]:
        gateway_status = self.runner.run_json(
            [self.bin, "gateway", "status", "--require-rpc", "--json"],
            timeout=self.SERVER_STATUS_TIMEOUT_SECONDS,
        )
        tg_bot_status = self.get_tg_bot_status()
        feishu_bot_status = self.get_feishu_bot_status()
        weixin_bot_status = self.get_weixin_bot_status()
        current_model_status = self.get_current_model()

        return {
            "ok": True,
            "check": "openclaw gateway status --require-rpc --json",
            "timeout_seconds": self.SERVER_STATUS_TIMEOUT_SECONDS,
            "config_path": str(self.config_path),
            "config_exists": self.config_path.exists(),
            "gateway_status": self._summarize_gateway_status(gateway_status),
            "tg_bot_status": tg_bot_status,
            "feishu_bot_status": feishu_bot_status,
            "weixin_bot_status": weixin_bot_status,
            "current_model_status": current_model_status,
        }

    def get_current_gateway_token(self) -> Dict[str, object]:
        config = self._load_config()
        gateway = config.get("gateway", {})
        auth = gateway.get("auth", {}) if isinstance(gateway, dict) else {}
        configured_mode = auth.get("mode") if isinstance(auth, dict) else None
        configured_token = auth.get("token") if isinstance(auth, dict) else None
        return {
            "ok": True,
            "gateway_auth_mode": configured_mode,
            "gateway_token": configured_token,
            "config_path": str(self.config_path),
            "config_exists": self.config_path.exists(),
        }

    def list_agents(self) -> Dict[str, object]:
        agents_payload = self.runner.run_json(
            [self.bin, "agents", "list", "--bindings", "--json"],
            timeout=self._agents_list_timeout_seconds(),
        )
        agents = [
            item
            for item in self._extract_agent_list(agents_payload)
            if item.get("id") != "main"
        ]
        return {
            "ok": True,
            "check": "openclaw agents list --bindings --json",
            "agent_count": len(agents),
            "agents": agents,
        }

    def _agents_list_timeout_seconds(self) -> int:
        if not self.container_runtime:
            return self.SERVER_STATUS_TIMEOUT_SECONDS
        variable = "UNITAG_AGENT_MANAGER_AGENTS_LIST_TIMEOUT_SECONDS"
        value = os.environ.get(variable)
        if value is None:
            return self.SERVER_STATUS_TIMEOUT_SECONDS
        if re.fullmatch(r"(?:[1-5][0-9]|60)", value) is None:
            raise ValueError(f"{variable} must be an ASCII integer between 10 and 60")
        return int(value)

    def _extract_agent_list(self, payload: Dict[str, object]) -> List[Dict[str, object]]:
        if isinstance(payload, list):
            return payload
        for key in ("agents", "list", "items", "payload"):
            value = payload.get(key) if isinstance(payload, dict) else None
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                nested = value.get("agents") or value.get("list") or value.get("items")
                if isinstance(nested, list):
                    return nested
        return []

    def _summarize_gateway_status(self, payload: Dict[str, object]) -> Dict[str, object]:
        if not isinstance(payload, dict):
            return {}
        summary: Dict[str, object] = {}
        for key in (
            "ok",
            "degraded",
            "status",
            "service",
            "runtime",
            "rpc",
            "url",
            "configuredUrl",
            "probe",
            "authWarning",
        ):
            if key in payload:
                summary[key] = payload[key]
        return summary or payload

    def _configure_gateway_auth(self, gateway_token: str) -> Dict[str, object]:
        config_path = self.config_path
        control_ui_origin = self._container_control_ui_origin()
        disable_device_auth = self.container_runtime
        if self.runner.dry_run:
            result: Dict[str, object] = {
                "skipped": True,
                "config_path": str(config_path),
                "gateway_auth_mode": "token",
            }
            if control_ui_origin is not None:
                result["control_ui_allowed_origins"] = [control_ui_origin]
            if disable_device_auth:
                result["control_ui_device_auth_disabled"] = True
            return result

        config = self._load_config()
        if not isinstance(config, dict):
            raise ValueError(f"Config must be a JSON object: {config_path}")

        gateway = config.setdefault("gateway", {})
        auth = gateway.setdefault("auth", {})
        auth["mode"] = "token"
        auth["token"] = gateway_token

        control_ui_allowed_origins = None
        if control_ui_origin is not None or disable_device_auth:
            control_ui = gateway.setdefault("controlUi", {})
            if not isinstance(control_ui, dict):
                raise ValueError("gateway.controlUi must be a JSON object")
            if control_ui_origin is not None:
                control_ui_allowed_origins = self._merge_control_ui_allowed_origins(
                    control_ui.get("allowedOrigins"),
                    control_ui_origin,
                )
                control_ui["allowedOrigins"] = control_ui_allowed_origins
            if disable_device_auth:
                control_ui["dangerouslyDisableDeviceAuth"] = True

        changed_paths = [
            "gateway.auth.mode",
            "gateway.auth.token",
        ]
        if control_ui_allowed_origins is not None:
            changed_paths.append("gateway.controlUi.allowedOrigins")
        if disable_device_auth:
            changed_paths.append("gateway.controlUi.dangerouslyDisableDeviceAuth")
        self._write_config(
            config,
            note="configure gateway auth for create_instance",
            changed_paths=changed_paths,
            extra={
                "gateway_auth_mode": "token",
            },
        )
        result = {
            "config_path": str(config_path),
            "gateway_auth_mode": "token",
        }
        if control_ui_allowed_origins is not None:
            result["control_ui_allowed_origins"] = control_ui_allowed_origins
        if disable_device_auth:
            result["control_ui_device_auth_disabled"] = True
        return result

    def _container_control_ui_origin(self) -> Optional[str]:
        if not self.container_runtime:
            return None
        value = os.environ.get("UNITAG_AGENT_MANAGER_CONTROL_UI_ORIGIN")
        if value is None or not value.strip():
            return None
        return self._normalize_control_ui_origin(value)

    @staticmethod
    def _normalize_control_ui_origin(value: object) -> str:
        if not isinstance(value, str) or value != value.strip() or not value:
            raise ValueError("UNITAG_AGENT_MANAGER_CONTROL_UI_ORIGIN must be an absolute http/https origin")
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError as exc:
            raise ValueError(
                "UNITAG_AGENT_MANAGER_CONTROL_UI_ORIGIN must be an absolute http/https origin"
            ) from exc
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.netloc
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or port is not None and not 0 < port < 65536
        ):
            raise ValueError("UNITAG_AGENT_MANAGER_CONTROL_UI_ORIGIN must be an absolute http/https origin")
        return urlunsplit((parsed.scheme.lower(), parsed.netloc, "", "", ""))

    def _merge_control_ui_allowed_origins(
        self,
        existing: object,
        additional_origin: str,
    ) -> List[str]:
        merged: List[str] = []
        values = existing if isinstance(existing, list) else []
        for item in values:
            try:
                origin = self._normalize_control_ui_origin(item)
            except ValueError:
                continue
            if origin not in merged:
                merged.append(origin)
        if additional_origin not in merged:
            merged.append(additional_origin)
        return merged

    def _preserve_gateway_auth(self) -> Dict[str, object]:
        config_path = self.config_path
        if self.runner.dry_run:
            return {
                "skipped": True,
                "config_path": str(config_path),
                "preserved": True,
            }

        config = self._load_config()
        gateway = config.get("gateway", {})
        auth = gateway.get("auth", {}) if isinstance(gateway, dict) else {}
        mode = auth.get("mode") if isinstance(auth, dict) else None
        token = auth.get("token") if isinstance(auth, dict) else None
        return {
            "config_path": str(config_path),
            "preserved": True,
            "gateway_auth_mode": mode,
            "has_gateway_token": isinstance(token, str) and bool(token.strip()),
        }

    def _configured_gateway_token(self) -> Optional[str]:
        config = self._load_config()
        gateway = config.get("gateway", {})
        auth = gateway.get("auth", {}) if isinstance(gateway, dict) else {}
        token = auth.get("token") if isinstance(auth, dict) else None
        if isinstance(token, str) and token.strip():
            return token.strip()
        return None

    def _restart_gateway_service(self) -> Dict[str, object]:
        context = getattr(self, "runtime_context", None)
        if context and context.lifecycle == "external":
            self.restart_required = True
            return {"deferred": True, "method": "external_controller", "runtime_id": context.runtime_id}
        if context and context.lifecycle != "legacy":
            # Verify native daemon/config ownership before asking it to restart.
            self._assert_refresh_gateway_scope()
            self._activate_refresh()
            return {"method": "scoped_gateway_restart", "runtime_id": context.runtime_id}
        if self.container_runtime:
            self.restart_required = True
            return {
                "deferred": True,
                "method": "container_restart_required",
                "service": self.GATEWAY_SERVICE_NAME,
            }

        if self.runner.dry_run:
            return {
                "skipped": True,
                "method": "systemctl_user_stop_start",
                "service": self.GATEWAY_SERVICE_NAME,
            }

        steps: List[Dict[str, object]] = []
        stop_result = self.runner.run(
            ["systemctl", "--user", "stop", self.GATEWAY_SERVICE_NAME],
            timeout=self.GATEWAY_STOP_TIMEOUT_SECONDS,
        )
        steps.append(self._command_step("systemctl.stop", stop_result))

        stopped_result = self._wait_gateway_process_stopped()
        steps.append(self._build_step_payload("gateway.wait_stopped", stopped_result))

        start_result = self.runner.run(
            ["systemctl", "--user", "start", self.GATEWAY_SERVICE_NAME],
            timeout=self.GATEWAY_STOP_TIMEOUT_SECONDS,
        )
        steps.append(self._command_step("systemctl.start", start_result))

        listening_result = self._wait_gateway_port_listening()
        steps.append(self._build_step_payload("gateway.wait_port", listening_result))

        return {
            "method": "systemctl_user_stop_start",
            "service": self.GATEWAY_SERVICE_NAME,
            "port": self.GATEWAY_PORT,
            "steps": steps,
        }

    def _wait_gateway_process_stopped(self) -> Dict[str, object]:
        started_at = perf_counter()
        checks = 0
        while True:
            checks += 1
            if not self._gateway_process_running():
                return {
                    "ok": True,
                    "checks": checks,
                    "elapsed_ms": round((perf_counter() - started_at) * 1000, 1),
                }
            if perf_counter() - started_at >= self.GATEWAY_STOP_TIMEOUT_SECONDS:
                raise TimeoutError(
                    f"Timed out waiting for {self.GATEWAY_SERVICE_NAME} process to stop"
                )
            sleep(self.GATEWAY_POLL_INTERVAL_SECONDS)

    def _wait_gateway_port_listening(self) -> Dict[str, object]:
        started_at = perf_counter()
        checks = 0
        while True:
            checks += 1
            if self._gateway_port_listening():
                return {
                    "ok": True,
                    "port": self.GATEWAY_PORT,
                    "checks": checks,
                    "elapsed_ms": round((perf_counter() - started_at) * 1000, 1),
                }
            if perf_counter() - started_at >= self.GATEWAY_START_TIMEOUT_SECONDS:
                raise TimeoutError(
                    f"Timed out waiting for gateway port {self.GATEWAY_PORT} to listen"
                )
            sleep(self.GATEWAY_POLL_INTERVAL_SECONDS)

    def _gateway_process_running(self) -> bool:
        try:
            self.runner.run(
                ["pgrep", "-f", "openclaw-gateway"],
                timeout=self.SERVER_STATUS_TIMEOUT_SECONDS,
            )
        except CommandError:
            return False
        return True

    def _gateway_port_listening(self) -> bool:
        try:
            result = self.runner.run(
                ["ss", "-ltn"],
                timeout=self.SERVER_STATUS_TIMEOUT_SECONDS,
            )
        except CommandError:
            return False
        port_pattern = re.compile(rf":{re.escape(self.GATEWAY_PORT)}(?:\s|$)")
        return any(port_pattern.search(line) for line in result.stdout.splitlines())
