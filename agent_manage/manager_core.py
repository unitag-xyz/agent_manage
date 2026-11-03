"""Shared state, configuration I/O, and execution primitives for managers."""

from __future__ import annotations

import json
import os
import secrets
import shutil
from pathlib import Path
from time import perf_counter
from typing import Dict, List, Optional

from .local import LocalRunner
from .error_details import command_error_details, embedded_error_payload
from .file_io import atomic_write_bytes, encode_json, stage_bytes
from .restart_policy import host_restart_required, standard_host_reload_available
from .settings import (
    DEFAULT_AI_SHOP,
    DEFAULT_IMAGE_QUALITY,
    DEFAULT_MODEL_ENV,
    IMAGE_QUALITY_CHOICES,
    MODEL_GATEWAYS,
    catalog_url_for_shop,
)


class ManagerCore:
    """Provide constants and cross-domain helpers used by manager mixins."""

    SERVER_STATUS_TIMEOUT_SECONDS = 10
    GATEWAY_SERVICE_NAME = "openclaw-gateway.service"
    GATEWAY_STOP_TIMEOUT_SECONDS = 30
    GATEWAY_START_TIMEOUT_SECONDS = 180
    GATEWAY_POLL_INTERVAL_SECONDS = 1.0
    GATEWAY_PORT = "18889"
    LIBRARY_VERIFY_TIMEOUT_SECONDS = 30
    LIBRARY_INSTALL_TIMEOUT_SECONDS = 600
    OPENCLAW_COMMAND_TIMEOUT_SECONDS = 180
    FEISHU_CHANNEL_ID = "feishu"
    FEISHU_DOMAINS = {"feishu", "lark"}
    WEIXIN_PLUGIN_ID = "openclaw-weixin"
    WEIXIN_PLUGIN_PACKAGE = "@tencent-weixin/openclaw-weixin"
    WEIXIN_DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
    MANAGED_MODEL_PROVIDER = "unipay-fun"
    OPENAI_MODEL_PROVIDER = "openai"
    IMAGE_MODEL_PROVIDER = OPENAI_MODEL_PROVIDER
    IMAGE_MODEL_ID = "gpt-image-2"
    IMAGE_MODEL_REF = "openai/gpt-image-2"
    IMAGE_GENERATION_POLICY_START = "<!-- agent_manage:image-generation-policy:start -->"
    IMAGE_GENERATION_POLICY_END = "<!-- agent_manage:image-generation-policy:end -->"
    RUNTIME_POLICY_START = "<!-- agent_manage:runtime-policy:start -->"
    RUNTIME_POLICY_END = "<!-- agent_manage:runtime-policy:end -->"
    DEFAULT_MODEL_ENV = DEFAULT_MODEL_ENV
    DEFAULT_AI_SHOP = DEFAULT_AI_SHOP
    DEFAULT_IMAGE_QUALITY = DEFAULT_IMAGE_QUALITY
    IMAGE_QUALITY_CHOICES = IMAGE_QUALITY_CHOICES
    LOCAL_TEMPLATE_ROOT = "~/.openclaw/templates"
    LOCAL_WORKSPACE_ROOT = "~/.openclaw/data"
    CONTAINER_OPENCLAW_ROOT = "/home/node/.openclaw"
    CONTAINER_TEMPLATE_ROOT = f"{CONTAINER_OPENCLAW_ROOT}/templates"
    CONTAINER_WORKSPACE_ROOT = f"{CONTAINER_OPENCLAW_ROOT}/data"
    MODEL_GATEWAYS = MODEL_GATEWAYS
    MODEL_CATALOG_URL = catalog_url_for_shop(
        MODEL_GATEWAYS[DEFAULT_MODEL_ENV]["catalog_url"],
        DEFAULT_AI_SHOP,
    )
    PREFERRED_PRIMARY_MODEL_IDS = (
        "deepseek-v4-flash",
        "deepseek-v4-pro",
        "gpt-5.4",
        "gpt-5.3-codex",
        "gpt-5.4-nano",
        "gpt-5.4-mini",
        "gpt-5-nano",
    )
    OPENCLAW_PROVIDER_CONFIG_KEYS = ("baseUrl", "api", "apiKey", "models")
    OPENCLAW_MODEL_DEFINITION_KEYS = (
        "id",
        "name",
        "contextWindow",
        "maxTokens",
        "input",
        "cost",
        "reasoning",
    )
    OPENCLAW_MODEL_COST_KEYS = ("input", "output", "cacheRead", "cacheWrite")
    OPENCLAW_MODEL_INPUT_TYPES = ("text", "image")

    def __init__(
        self,
        runner: LocalRunner,
        template_root: Optional[str] = None,
        config_path: Optional[str] = None,
    ) -> None:
        self.runner = runner
        self.container_runtime = os.environ.get("UNITAG_AGENT_MANAGER_RUNTIME") == "container"
        self.restart_required = False
        self.bin = runner.openclaw_bin
        if self.container_runtime:
            template_root = template_root or self.CONTAINER_TEMPLATE_ROOT
            config_path = config_path or f"{self.CONTAINER_OPENCLAW_ROOT}/openclaw.json"
        self.template_root = (
            Path(template_root).expanduser().resolve()
            if template_root
            else Path("~/template").expanduser().resolve()
        )
        self.template_root_explicit = template_root is not None
        self.config_path = (
            Path(config_path).expanduser().resolve()
            if config_path
            else Path.home() / ".openclaw" / "openclaw.json"
        )

    def _dedupe_preserve_order(self, values: List[str]) -> List[str]:
        result = []
        seen = set()
        for value in values:
            if value in seen:
                continue
            seen.add(value)
            result.append(value)
        return result

    def _run_timed_step(self, steps: List[Dict[str, object]], step: str, func):
        self.runner.log(f"step: start {step}")
        started_at = perf_counter()
        result = func()
        elapsed_ms = round((perf_counter() - started_at) * 1000, 1)
        self.runner.log(f"step: done {step} ({elapsed_ms} ms)")
        steps.append(
            self._build_step_payload(
                step,
                self._step_result_for_response(step, result),
                elapsed_ms=elapsed_ms,
            )
        )
        return result

    @staticmethod
    def _elapsed_ms(started_at: float) -> float:
        return round((perf_counter() - started_at) * 1000, 1)

    def _run_timed_rollback_step(self, rollback_steps: List[Dict[str, object]], func) -> None:
        started_at = perf_counter()
        result = func()
        if isinstance(result, dict):
            result["elapsed_ms"] = self._elapsed_ms(started_at)
        rollback_steps.append(result)

    def _step_result_for_response(self, step: str, result: object) -> object:
        """Keep catalog data available internally without returning it in CLI steps."""
        if step != "models.fetch_catalog" or not isinstance(result, dict):
            return result

        models_config = result.get("models_config")
        providers = models_config.get("providers") if isinstance(models_config, dict) else None
        provider_names = (
            sorted(str(name) for name in providers)
            if isinstance(providers, dict)
            else sorted(
                {
                    str(model.get("provider"))
                    for model in result.get("models", [])
                    if isinstance(model, dict) and model.get("provider")
                }
            )
        )
        return {
            "source_url": result.get("source_url"),
            "model_count": result.get("model_count"),
            "primary_model": result.get("primary_model"),
            "official_image_model_available": bool(
                result.get("official_image_model_available", False)
            ),
            "provider_names": provider_names,
            "provider_count": len(provider_names),
        }

    def _build_step_payload(
        self,
        step: str,
        result: Dict[str, object],
        elapsed_ms: Optional[float] = None,
    ) -> Dict[str, object]:
        payload: Dict[str, object] = {"step": step, "result": result}
        if elapsed_ms is not None:
            payload["elapsed_ms"] = elapsed_ms
        return payload

    def _scoped_step_name(self, step: str, scope: Optional[str]) -> str:
        return f"{step}[{scope}]" if scope else step

    def _index_step_results(self, steps: List[Dict[str, object]]) -> Dict[str, Dict[str, object]]:
        indexed: Dict[str, Dict[str, object]] = {}
        for item in steps:
            step = item.get("step")
            result = item.get("result")
            if isinstance(step, str) and isinstance(result, dict):
                indexed[step] = result
        return indexed

    _embedded_error_payload = staticmethod(embedded_error_payload)
    _error_details = staticmethod(command_error_details)

    def _command_step(self, step: str, result) -> Dict[str, object]:
        payload = self._command_result_payload(result)
        return {"step": step, "result": payload}

    def _command_result_payload(self, result) -> Dict[str, object]:
        payload = {
            "command": result.command_text,
            "returncode": result.returncode,
            "skipped": result.skipped,
        }
        if result.stdout.strip():
            payload["stdout"] = result.stdout
        if result.stderr.strip():
            payload["stderr"] = result.stderr
        return payload

    def _generate_gateway_token(self) -> str:
        return secrets.token_urlsafe(32)

    def _container_gateway_token(self) -> Optional[str]:
        if not self.container_runtime:
            return None
        token = os.environ.get("OPENCLAW_GATEWAY_TOKEN")
        return token.strip() if isinstance(token, str) and token.strip() else None

    def _host_reload_available(self) -> bool:
        return standard_host_reload_available(self.bin, container_runtime=self.container_runtime)

    def _instance_restart_required(self, snapshot, *, legacy_required: bool = True) -> bool:
        if self.restart_required:
            return True
        if not self._host_reload_available():
            return legacy_required
        if self.runner.dry_run:
            return False
        if snapshot is None:
            return True
        try:
            before = json.loads(snapshot[0])
            if not isinstance(before, dict):
                return True
            after = self._load_config()
            return host_restart_required(before, after)
        except (OSError, ValueError, TypeError):
            return True

    def _snapshot_config_file(self) -> tuple[bytes, int] | None:
        if self.runner.dry_run or not self.config_path.exists():
            return None
        return (
            self.config_path.read_bytes(),
            self.config_path.stat().st_mode & 0o777,
        )

    def _safe_restore_config_snapshot(
        self,
        snapshot: tuple[bytes, int],
    ) -> Dict[str, object]:
        try:
            atomic_write_bytes(self.config_path, snapshot[0], snapshot[1])
            return {
                "step": "rollback.config.restore",
                "result": {
                    "restored": True,
                    "path": str(self.config_path),
                },
            }
        except Exception as exc:
            return {
                "step": "rollback.config.restore",
                "error": str(exc),
            }

    def _load_config(self) -> Dict[str, object]:
        self.runner.log(f"config: load {self.config_path}")
        if not self.config_path.exists():
            raise FileNotFoundError(f"Config file not found: {self.config_path}")
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError(f"Config must be a JSON object: {self.config_path}")
        return config

    def _write_config(
        self,
        config: Dict[str, object],
        note: str,
        changed_paths: Optional[List[str]] = None,
        extra: Optional[Dict[str, object]] = None,
    ) -> Dict[str, object]:
        changed_paths = changed_paths or []
        payload = {
            "config_path": str(self.config_path),
            "changed_paths": changed_paths,
        }
        if extra:
            payload.update(extra)
        self.runner.log(f"config: write {self.config_path} ({note})")
        if self.runner.dry_run:
            payload["skipped"] = True
            payload["note"] = note
            return payload

        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        backup_path = self.config_path.with_suffix(self.config_path.suffix + ".bak")
        tmp_path = stage_bytes(self.config_path, encode_json(config))
        try:
            if self.config_path.exists():
                shutil.copy2(self.config_path, backup_path)
                os.chmod(backup_path, 0o600)
            tmp_path.replace(self.config_path)
        finally:
            tmp_path.unlink(missing_ok=True)
        payload["backup_path"] = str(backup_path) if backup_path.exists() else None
        return payload
