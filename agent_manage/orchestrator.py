from __future__ import annotations

import json
import secrets
import shutil
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from time import perf_counter, sleep
from typing import Dict, List, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

from .local import CommandError, LocalRunner

from .models import (
    AddAgentRequest,
    AddAgentsRequest,
    AddTelegramBotRequest,
    AddWeixinBotRequest,
    CreateInstanceRequest,
    DeleteTelegramBotRequest,
    DeleteWeixinBotRequest,
    SetModelRequest,
)


class InstanceManagerV2:
    SERVER_STATUS_TIMEOUT_SECONDS = 10
    GATEWAY_SERVICE_NAME = "openclaw-gateway.service"
    GATEWAY_STOP_TIMEOUT_SECONDS = 30
    GATEWAY_START_TIMEOUT_SECONDS = 180
    GATEWAY_POLL_INTERVAL_SECONDS = 1.0
    GATEWAY_PORT = "18889"
    LIBRARY_VERIFY_TIMEOUT_SECONDS = 30
    LIBRARY_INSTALL_TIMEOUT_SECONDS = 600
    WEIXIN_PLUGIN_ID = "openclaw-weixin"
    WEIXIN_PLUGIN_PACKAGE = "@tencent-weixin/openclaw-weixin"
    WEIXIN_DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
    MANAGED_MODEL_PROVIDER = "unipay-fun"
    MODEL_CATALOG_URL = "https://unitag.dola.fi/aigateway/api/frontend/aimodels"
    DEFAULT_MODEL_MAX_TOKENS = 128000
    PREFERRED_PRIMARY_MODEL_IDS = (
        "deepseek-v4-flash",
        "deepseek-v4-pro",
        "gpt-5.4",
        "gpt-5.3-codex",
        "gpt-5.4-nano",
        "gpt-5.4-mini",
        "gpt-5-nano",
    )

    def __init__(
        self,
        runner: LocalRunner,
        template_root: Optional[str] = None,
        config_path: Optional[str] = None,
    ) -> None:
        self.runner = runner
        self.bin = runner.openclaw_bin
        self.template_root = (
            Path(template_root).expanduser().resolve()
            if template_root
            else Path("~/template").expanduser().resolve()
        )
        self.config_path = (
            Path(config_path).expanduser().resolve()
            if config_path
            else Path.home() / ".openclaw" / "openclaw.json"
        )

    def create_instance(self, request: CreateInstanceRequest) -> Dict[str, object]:
        if not request.model_key.strip():
            raise ValueError("model_key is required")
        steps: List[Dict[str, object]] = []
        agent_name = self.resolve_agent_name(request)
        workspace = self.default_workspace(agent_name, request.workspace_root)
        archive_path = self.resolve_archive_path(request)
        template_dir = self.resolve_template_dir(request)
        created_agent = False
        started_template_prepare = False
        created_workspace = False

        try:
            provision_result = self._provision_agent_from_template(
                steps=steps,
                template_name=request.template_name,
                agent_name=agent_name,
                archive_path=archive_path,
                template_dir=template_dir,
                workspace=workspace,
                model=request.model,
                rollback_on_fail=request.rollback_on_fail,
                step_scope=None,
            )
            created_agent = bool(provision_result["created_agent"])
            started_template_prepare = bool(provision_result["started_template_prepare"])
            created_workspace = bool(provision_result["created_workspace"])

            fetched_models = self._run_timed_step(
                steps,
                "models.fetch_catalog",
                self._fetch_supported_gateway_models,
            )

            models_result = self._run_timed_step(
                steps,
                "config.configure_models",
                lambda: self._configure_config_models(
                    model_key=request.model_key,
                    supported_models=fetched_models["models"],
                ),
            )

            gateway_token = self._generate_gateway_token()
            gateway_auth_result = self._run_timed_step(
                steps,
                "config.configure_gateway_auth",
                lambda: self._configure_gateway_auth(gateway_token),
            )

            tools_result = self._run_timed_step(
                steps,
                "config.configure_tools",
                self._configure_config_tools,
            )

            return {
                "ok": True,
                "template_name": request.template_name,
                "agent_name": agent_name,
                "gateway_token": gateway_token,
                "workspace": str(workspace),
                "archive_path": str(archive_path),
                "template_dir": str(template_dir) if template_dir else None,
                "steps": steps,
            }
        except Exception as exc:
            payload = self._embedded_error_payload(exc)
            if payload.get("rollback"):
                raise
            rollback_steps: List[Dict[str, object]] = []
            if request.rollback_on_fail:
                if created_workspace or (created_agent and workspace.exists()):
                    rollback_steps.append(self._safe_purge_workspace(workspace))
                if created_agent:
                    rollback_steps.append(self._safe_delete_agent(agent_name))
                if started_template_prepare and template_dir.exists():
                    rollback_steps.append(self._safe_purge_template_dir(template_dir))
            raise RuntimeError(
                json.dumps(
                    {
                        "error": str(exc),
                        "details": self._error_details(exc),
                        "steps": steps,
                        "rollback": rollback_steps,
                    },
                    ensure_ascii=False,
                )
            ) from exc

    def add_agents(self, request: AddAgentsRequest) -> Dict[str, object]:
        if not request.agents:
            raise ValueError("agents is required")

        steps: List[Dict[str, object]] = []
        agent_results: List[Dict[str, object]] = []
        seen_agent_names = set()

        for spec in request.agents:
            agent_name = spec.agent_name.strip()
            if not agent_name:
                raise ValueError("agent_name is required")
            if agent_name in seen_agent_names:
                raise ValueError(f"Duplicate agent_name in batch: {agent_name}")
            seen_agent_names.add(agent_name)

            template_name = self.resolve_add_agent_template_name(spec)
            workspace = self.resolve_add_agent_workspace(spec, request.workspace_root)
            archive_path = self.template_root / f"{template_name}.zip"
            template_dir = self.template_root / template_name

            self._provision_agent_from_template(
                steps=steps,
                template_name=template_name,
                agent_name=agent_name,
                archive_path=archive_path,
                template_dir=template_dir,
                workspace=workspace,
                model=spec.model,
                rollback_on_fail=True,
                step_scope=agent_name,
            )

            agent_results.append(
                {
                    "agent_name": agent_name,
                    "template_name": template_name,
                    "workspace": str(workspace),
                    "archive_path": str(archive_path),
                    "template_dir": str(template_dir),
                    "model": spec.model,
                }
            )
        step_results = self._index_step_results(steps)
        added_count = 0
        skipped_count = 0
        for item in agent_results:
            agent_name = item["agent_name"]
            add_result = step_results.get(self._scoped_step_name("agents.add", agent_name), {})
            status = "skipped" if add_result.get("skipped") else "added"
            item["status"] = status
            item["result"] = {
                "template_prepare": step_results.get(
                    self._scoped_step_name("template.prepare", agent_name),
                    {},
                ),
                "libraries_ensure": step_results.get(
                    self._scoped_step_name("libraries.ensure", agent_name),
                    {},
                ),
                "common_skills_install": step_results.get(
                    self._scoped_step_name("common_skills.install", agent_name),
                    {},
                ),
                "agents_add": add_result,
                "workspace_populate": step_results.get(
                    self._scoped_step_name("workspace.populate", agent_name),
                    {},
                ),
            }
            if status == "added":
                added_count += 1
            else:
                skipped_count += 1

        return {
            "ok": True,
            "requested_count": len(request.agents),
            "added_count": added_count,
            "skipped_count": skipped_count,
            "restart_required": False,
            "post_batch_actions": [],
            "agents": agent_results,
            "steps": steps,
        }

    def add_tg_bot(self, request: AddTelegramBotRequest) -> Dict[str, object]:
        self._ensure_agent_exists_in_config(request.agent_name)

        account_id = request.bot_name or self._generate_tg_bot_name()
        config = self._load_config()
        telegram = config.setdefault("channels", {}).setdefault("telegram", {})
        telegram["enabled"] = True
        accounts = telegram.setdefault("accounts", {})
        accounts[account_id] = {
            "botToken": request.bot_token,
            "dmPolicy": "open",
            "allowFrom": ["*"],
        }

        bindings = list(config.get("bindings", []))
        filtered = []
        removed = 0
        for item in bindings:
            match = item.get("match", {})
            if (
                match.get("channel") == "telegram"
                and match.get("accountId") == account_id
            ):
                removed += 1
                continue
            filtered.append(item)
        filtered.append(
            {
                "agentId": request.agent_name,
                "match": {
                    "channel": "telegram",
                    "accountId": account_id,
                },
            }
        )
        config["bindings"] = filtered

        write_result = self._write_config(
            config,
            note=f"add telegram bot {account_id} for agent {request.agent_name}",
            changed_paths=[
                f"channels.telegram.accounts.{account_id}",
                "bindings",
            ],
            extra={
                "generated_bot_name": request.bot_name is None,
                "binding_agent": request.agent_name,
                "removed_existing_bindings": removed,
            },
        )
        restart_result = self._restart_gateway_service()
        return {
            "ok": True,
            "agent_name": request.agent_name,
            "bot_name": account_id,
            "config_write": write_result,
            "gateway_restart": self._build_step_payload("gateway.restart", restart_result),
        }

    def add_weixin_bot(self, request: AddWeixinBotRequest) -> Dict[str, object]:
        self._ensure_agent_exists_in_config(request.agent_name)
        normalized_account_id = self._normalize_weixin_account_id(request.ilink_bot_id)
        plugin_prepare = self._prepare_weixin_plugin_config()
        stale_accounts = self._clear_stale_weixin_accounts_for_user(
            current_account_id=normalized_account_id,
            user_id=request.ilink_user_id,
        )
        state_result = self._write_weixin_account_state(
            account_id=normalized_account_id,
            bot_token=request.bot_token,
            base_url=request.baseurl or self.WEIXIN_DEFAULT_BASE_URL,
            user_id=request.ilink_user_id,
        )

        config = self._load_config()
        channels = config.setdefault("channels", {})
        weixin = channels.setdefault(self.WEIXIN_PLUGIN_ID, {})
        accounts = weixin.setdefault("accounts", {})
        account_config = dict(accounts.get(normalized_account_id, {}))
        account_config["enabled"] = True
        if request.bot_name:
            account_config["name"] = request.bot_name
        if request.route_tag:
            account_config["routeTag"] = request.route_tag
        if request.cdn_base_url:
            account_config["cdnBaseUrl"] = request.cdn_base_url
        accounts[normalized_account_id] = account_config
        weixin["channelConfigUpdatedAt"] = self._channel_timestamp()

        bindings = list(config.get("bindings", []))
        filtered = []
        removed = 0
        for item in bindings:
            match = item.get("match", {})
            if (
                match.get("channel") == self.WEIXIN_PLUGIN_ID
                and match.get("accountId") == normalized_account_id
            ):
                removed += 1
                continue
            filtered.append(item)
        filtered.append(
            {
                "agentId": request.agent_name,
                "match": {
                    "channel": self.WEIXIN_PLUGIN_ID,
                    "accountId": normalized_account_id,
                },
            }
        )
        config["bindings"] = filtered

        write_result = self._write_config(
            config,
            note=f"add weixin bot {normalized_account_id} for agent {request.agent_name}",
            changed_paths=[
                f"channels.{self.WEIXIN_PLUGIN_ID}.accounts.{normalized_account_id}",
                f"channels.{self.WEIXIN_PLUGIN_ID}.channelConfigUpdatedAt",
                "bindings",
            ],
            extra={
                "binding_agent": request.agent_name,
                "removed_existing_bindings": removed,
            },
        )

        restart_result = self._restart_gateway_service()
        plugin_prepare.setdefault("steps", []).append(
            self._build_step_payload("gateway.restart", restart_result)
        )

        return {
            "ok": True,
            "agent_name": request.agent_name,
            "account_id": normalized_account_id,
            "raw_account_id": request.ilink_bot_id,
            "plugin_prepare": plugin_prepare,
            "stale_accounts_cleared": stale_accounts,
            "state_write": state_result,
            "config_write": write_result,
        }

    def check_server_status(self) -> Dict[str, object]:
        gateway_status = self.runner.run_json(
            [self.bin, "gateway", "status", "--require-rpc", "--json"],
            timeout=self.SERVER_STATUS_TIMEOUT_SECONDS,
        )
        tg_bot_status = self.get_tg_bot_status()
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
            "weixin_bot_status": weixin_bot_status,
            "current_model_status": current_model_status,
        }

    def get_tg_bot_status(self) -> Dict[str, object]:
        config = self._load_config()
        telegram = config.get("channels", {}).get("telegram", {})
        accounts = telegram.get("accounts", {})
        bindings = list(config.get("bindings", []))

        binding_counts: Dict[str, int] = {}
        for item in bindings:
            match = item.get("match", {})
            if match.get("channel") != "telegram":
                continue
            account_id = match.get("accountId")
            if not account_id:
                continue
            binding_counts[account_id] = binding_counts.get(account_id, 0) + 1

        bots: List[Dict[str, object]] = []
        for account_id in sorted(accounts.keys()):
            account = accounts.get(account_id, {})
            bound_count = binding_counts.get(account_id, 0)
            bots.append(
                {
                    "bot_name": account_id,
                    "enabled": bool(telegram.get("enabled", False)),
                    "binding_count": bound_count,
                    "is_bound": bound_count > 0,
                    "dm_policy": account.get("dmPolicy"),
                }
            )

        return {
            "ok": True,
            "telegram_enabled": bool(telegram.get("enabled", False)),
            "tg_bot_count": len(accounts),
            "bound_tg_bot_count": sum(1 for item in bots if item["is_bound"]),
            "total_binding_count": sum(binding_counts.values()),
            "bots": bots,
        }

    def delete_tg_bot(self, request: DeleteTelegramBotRequest) -> Dict[str, object]:
        config = self._load_config()
        telegram = config.setdefault("channels", {}).setdefault("telegram", {})
        accounts = telegram.setdefault("accounts", {})
        if request.bot_name not in accounts:
            raise FileNotFoundError(f"Telegram account '{request.bot_name}' not found")

        accounts.pop(request.bot_name, None)
        bindings = list(config.get("bindings", []))
        filtered = []
        removed_bindings = 0
        for item in bindings:
            match = item.get("match", {})
            if (
                match.get("channel") == "telegram"
                and match.get("accountId") == request.bot_name
            ):
                removed_bindings += 1
                continue
            filtered.append(item)
        config["bindings"] = filtered
        telegram["enabled"] = bool(accounts)

        write_result = self._write_config(
            config,
            note=f"delete telegram bot {request.bot_name}",
            changed_paths=[
                f"channels.telegram.accounts.{request.bot_name}",
                "bindings",
                "channels.telegram.enabled",
            ],
            extra={
                "deleted_bot_name": request.bot_name,
                "removed_bindings": removed_bindings,
                "remaining_tg_bot_count": len(accounts),
            },
        )
        return {
            "ok": True,
            "deleted_bot_name": request.bot_name,
            "removed_bindings": removed_bindings,
            "remaining_tg_bot_count": len(accounts),
            "config_write": write_result,
        }

    def get_weixin_bot_status(self) -> Dict[str, object]:
        config = self._load_config()
        weixin = config.get("channels", {}).get(self.WEIXIN_PLUGIN_ID, {})
        accounts = weixin.get("accounts", {})
        bindings = list(config.get("bindings", []))

        binding_counts: Dict[str, int] = {}
        for item in bindings:
            match = item.get("match", {})
            if match.get("channel") != self.WEIXIN_PLUGIN_ID:
                continue
            account_id = match.get("accountId")
            if not account_id:
                continue
            binding_counts[account_id] = binding_counts.get(account_id, 0) + 1

        bots: List[Dict[str, object]] = []
        for account_id in sorted(accounts.keys()):
            account = accounts.get(account_id, {})
            state_payload = self._load_weixin_account_state(account_id)
            bound_count = binding_counts.get(account_id, 0)
            bots.append(
                {
                    "account_id": account_id,
                    "bot_name": account.get("name"),
                    "enabled": bool(account.get("enabled", True)),
                    "binding_count": bound_count,
                    "is_bound": bound_count > 0,
                    "route_tag": account.get("routeTag"),
                    "cdn_base_url": account.get("cdnBaseUrl"),
                    "has_state_file": state_payload is not None,
                    "state_baseurl": state_payload.get("baseUrl") if state_payload else None,
                    "ilink_user_id": state_payload.get("userId") if state_payload else None,
                }
            )

        return {
            "ok": True,
            "weixin_bot_count": len(accounts),
            "bound_weixin_bot_count": sum(1 for item in bots if item["is_bound"]),
            "total_binding_count": sum(binding_counts.values()),
            "bots": bots,
        }

    def delete_weixin_bot(self, request: DeleteWeixinBotRequest) -> Dict[str, object]:
        normalized_account_id = self._normalize_weixin_account_id(request.ilink_bot_id)
        config = self._load_config()
        channels = config.setdefault("channels", {})
        weixin = channels.setdefault(self.WEIXIN_PLUGIN_ID, {})
        accounts = weixin.setdefault("accounts", {})
        if normalized_account_id not in accounts:
            raise FileNotFoundError(
                f"Weixin account '{request.ilink_bot_id}' not found"
            )

        accounts.pop(normalized_account_id, None)
        bindings = list(config.get("bindings", []))
        filtered = []
        removed_bindings = 0
        for item in bindings:
            match = item.get("match", {})
            if (
                match.get("channel") == self.WEIXIN_PLUGIN_ID
                and match.get("accountId") == normalized_account_id
            ):
                removed_bindings += 1
                continue
            filtered.append(item)
        config["bindings"] = filtered
        weixin["channelConfigUpdatedAt"] = self._channel_timestamp()

        write_result = self._write_config(
            config,
            note=f"delete weixin bot {normalized_account_id}",
            changed_paths=[
                f"channels.{self.WEIXIN_PLUGIN_ID}.accounts.{normalized_account_id}",
                f"channels.{self.WEIXIN_PLUGIN_ID}.channelConfigUpdatedAt",
                "bindings",
            ],
            extra={
                "deleted_account_id": normalized_account_id,
                "removed_bindings": removed_bindings,
            },
        )
        state_delete = self._delete_weixin_account_state(normalized_account_id)
        return {
            "ok": True,
            "deleted_account_id": normalized_account_id,
            "raw_account_id": request.ilink_bot_id,
            "removed_bindings": removed_bindings,
            "remaining_weixin_bot_count": len(accounts),
            "state_delete": state_delete,
            "config_write": write_result,
        }

    def set_model(self, request: SetModelRequest) -> Dict[str, object]:
        supported_model_refs = self._supported_model_refs_from_config()
        if request.model_ref not in supported_model_refs:
            allowed = ", ".join(sorted(supported_model_refs))
            raise ValueError(f"Unsupported model '{request.model_ref}'. Allowed: {allowed}")

        set_result = self.runner.run([self.bin, "models", "set", request.model_ref])
        restart_result = self._restart_gateway_service()

        return {
            "ok": True,
            "model_ref": request.model_ref,
            "steps": [
                self._command_step("models.set", set_result),
            ],
            "gateway_restart": self._build_step_payload("gateway.restart", restart_result),
        }

    def get_current_model(self) -> Dict[str, object]:
        config = self._load_config()
        defaults = config.get("agents", {}).get("defaults", {})
        configured_default_model = (
            defaults.get("model", {}).get("primary")
            if isinstance(defaults.get("model"), dict)
            else None
        )
        agent_overrides = []
        for agent in config.get("agents", {}).get("list", []):
            if not isinstance(agent, dict):
                continue
            agent_id = agent.get("id")
            if agent_id == "main":
                continue
            model = agent.get("model", {})
            if isinstance(model, dict) and model.get("primary"):
                agent_overrides.append(
                    {
                        "agent_id": agent_id,
                        "model": model.get("primary"),
                    }
                )
        return {
            "ok": True,
            "current_model": configured_default_model,
            "configured_default_model": configured_default_model,
            "agent_overrides": agent_overrides,
            "config_path": str(self.config_path),
            "config_exists": self.config_path.exists(),
        }

    def get_supported_models(self) -> Dict[str, object]:
        config = self._load_config()
        defaults = config.get("agents", {}).get("defaults", {})
        current_model = (
            defaults.get("model", {}).get("primary")
            if isinstance(defaults.get("model"), dict)
            else None
        )
        models = self._supported_models_from_config(config)

        return {
            "ok": True,
            "provider": self.MANAGED_MODEL_PROVIDER,
            "current_model": current_model,
            "supported_model_refs": [item["model_ref"] for item in models],
            "models": models,
            "config_path": str(self.config_path),
            "config_exists": self.config_path.exists(),
        }

    def update_model_catalog(self) -> Dict[str, object]:
        config = self._load_config()
        current_model = self._configured_default_model_from_config(config)
        model_key = self._configured_model_api_key_from_config(config)
        steps: List[Dict[str, object]] = []

        fetched_models = self._run_timed_step(
            steps,
            "models.fetch_catalog",
            self._fetch_supported_gateway_models,
        )
        configure_result = self._run_timed_step(
            steps,
            "config.configure_models",
            lambda: self._configure_config_models(
                model_key=model_key,
                supported_models=fetched_models["models"],
                primary_model=current_model,
            ),
        )

        return {
            "ok": True,
            "provider": self.MANAGED_MODEL_PROVIDER,
            "current_model_before": current_model,
            "current_model_after": configure_result["primary_model"],
            "supported_model_refs": configure_result["managed_models"],
            "steps": steps,
            "config_path": str(self.config_path),
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
            timeout=self.SERVER_STATUS_TIMEOUT_SECONDS,
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

    def resolve_agent_name(self, request: CreateInstanceRequest) -> str:
        return request.template_name

    def default_workspace(self, agent_name: str, workspace_root: str) -> Path:
        return Path(workspace_root).expanduser().resolve() / agent_name

    def resolve_add_agent_template_name(self, request: AddAgentRequest) -> str:
        return (request.template_name or request.agent_name).strip()

    def resolve_add_agent_workspace(self, request: AddAgentRequest, workspace_root: str) -> Path:
        if request.workspace:
            return Path(request.workspace).expanduser().resolve()
        return self.default_workspace(request.agent_name, workspace_root)

    def resolve_archive_path(self, request: CreateInstanceRequest) -> Path:
        return self.template_root / f"{request.template_name}.zip"

    def resolve_template_dir(self, request: CreateInstanceRequest) -> Path:
        return self.template_root / request.template_name

    def _ensure_sources_ready(self, archive_path: Path) -> None:
        if not archive_path.is_file():
            raise FileNotFoundError(f"Template archive not found: {archive_path}")

    def _workspace_has_content(self, workspace: Path) -> bool:
        if not workspace.exists():
            return False
        if not workspace.is_dir():
            raise NotADirectoryError(f"Workspace path is not a directory: {workspace}")
        return any(workspace.iterdir())

    def _agent_exists(self, agent_name: str) -> bool:
        if self.runner.dry_run:
            return False
        config = self._load_config()
        for item in self._extract_agent_list(config):
            if item.get("id") == agent_name:
                return True
        return False

    def _ensure_agent_exists(self, agent_name: str) -> None:
        if self.runner.dry_run or self._agent_exists(agent_name):
            return
        raise FileNotFoundError(f"Agent not found: {agent_name}")

    def _ensure_agent_exists_in_config(self, agent_name: str) -> None:
        if self.runner.dry_run:
            return
        config = self._load_config()
        for item in self._extract_agent_list(config):
            if item.get("id") == agent_name:
                return
        raise FileNotFoundError(f"Agent not found in config: {agent_name}")

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

    def _extract_plugin_list(self, payload: Dict[str, object]) -> List[Dict[str, object]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if not isinstance(payload, dict):
            return []
        for key in ("plugins", "list", "items", "payload"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                nested = value.get("plugins") or value.get("list") or value.get("items")
                if isinstance(nested, list):
                    return [item for item in nested if isinstance(item, dict)]
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

    def _add_agent(self, agent_name: str, workspace: Path, model: Optional[str]) -> Dict[str, object]:
        args = [
            self.bin,
            "agents",
            "add",
            agent_name,
            "--workspace",
            str(workspace),
            "--non-interactive",
            "--json",
        ]
        if model:
            args.extend(["--model", model])
        result = self.runner.run(args)
        payload = self.runner._extract_json(result.stdout) if result.stdout.strip() else {}
        payload["command"] = result.command_text
        payload["returncode"] = result.returncode
        if result.skipped:
            payload["skipped"] = True
        return payload

    def _prepare_template_dir(self, archive_path: Path, template_dir: Path) -> Dict[str, object]:
        if self.runner.dry_run:
            return {
                "skipped": True,
                "archive_path": str(archive_path),
                "template_dir": str(template_dir),
            }

        if template_dir.exists():
            if not template_dir.is_dir():
                raise NotADirectoryError(f"Template path is not a directory: {template_dir}")
            shutil.rmtree(template_dir)

        with tempfile.TemporaryDirectory() as tmpdir:
            extract_dir = Path(tmpdir)
            shutil.unpack_archive(str(archive_path), str(extract_dir))
            source_root = self._resolve_unpacked_root(extract_dir)
            template_dir.mkdir(parents=True, exist_ok=False)
            copied = self._copy_directory_contents(source_root, template_dir)
        return {
            "template_dir": str(template_dir),
            "archive_path": str(archive_path),
            "copied_into_template_dir": copied,
        }

    def _populate_workspace(self, template_dir: Path, workspace: Path) -> Dict[str, object]:
        if self.runner.dry_run:
            return {
                "skipped": True,
                "template_dir": str(template_dir),
                "workspace": str(workspace),
            }
        if workspace.exists() and not workspace.is_dir():
            raise NotADirectoryError(f"Workspace path is not a directory: {workspace}")
        workspace.mkdir(parents=True, exist_ok=True)
        copied = self._copy_directory_contents(template_dir, workspace)

        return {
            "template_dir": str(template_dir),
            "workspace": str(workspace),
            "copied_from_template_dir": copied,
        }

    def _load_template_manifest(self, template_dir: Path) -> Dict[str, object]:
        manifest_path = template_dir / "template.yaml"
        if not manifest_path.is_file():
            return {}
        text = manifest_path.read_text(encoding="utf-8")
        try:
            import yaml  # type: ignore
        except ImportError:
            return self._parse_template_manifest_yaml_subset(text)

        payload = yaml.safe_load(text)
        if payload is None:
            return {}
        if not isinstance(payload, dict):
            raise ValueError(f"Template manifest must be a YAML object: {manifest_path}")
        return payload

    def _parse_template_manifest_yaml_subset(self, text: str) -> Dict[str, object]:
        manifest: Dict[str, object] = {}
        current_key: Optional[str] = None
        current_item: Optional[Dict[str, object]] = None
        list_keys = {"commonSkillFolders", "requiredLibraries"}

        for raw_line in text.splitlines():
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            indent = len(raw_line) - len(raw_line.lstrip(" "))
            if indent == 0:
                current_item = None
                if ":" not in stripped:
                    current_key = None
                    continue
                key, value = stripped.split(":", 1)
                key = key.strip()
                value = value.strip()
                if key in list_keys and value == "":
                    manifest[key] = []
                    current_key = key
                else:
                    manifest[key] = self._parse_simple_yaml_value(value)
                    current_key = None
                continue
            if current_key not in list_keys:
                continue
            if indent == 2 and stripped.startswith("- "):
                current_item = {}
                manifest.setdefault(current_key, [])
                items = manifest[current_key]
                if isinstance(items, list):
                    items.append(current_item)
                inline = stripped[2:].strip()
                if inline and ":" in inline:
                    key, value = inline.split(":", 1)
                    current_item[key.strip()] = self._parse_simple_yaml_value(value.strip())
                continue
            if current_item is not None and indent >= 4 and ":" in stripped:
                key, value = stripped.split(":", 1)
                current_item[key.strip()] = self._parse_simple_yaml_value(value.strip())
        return manifest

    def _parse_simple_yaml_value(self, value: str):
        if value == "":
            return ""
        lowered = value.lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        if lowered in {"null", "none"}:
            return None
        if value.startswith('"') and value.endswith('"'):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value[1:-1]
        if value.startswith("'") and value.endswith("'"):
            return value[1:-1]
        return value

    def _required_libraries_from_manifest(
        self,
        manifest: Dict[str, object],
    ) -> List[Dict[str, object]]:
        libraries = manifest.get("requiredLibraries")
        if not isinstance(libraries, list):
            return []
        return [item for item in libraries if isinstance(item, dict)]

    def _common_skill_sources_from_manifest(
        self,
        template_dir: Path,
        manifest: Dict[str, object],
    ) -> List[Path]:
        sources: List[Path] = []
        seen = set()

        def add_source(path_value: object) -> None:
            if not isinstance(path_value, str) or not path_value.strip():
                return
            source = (template_dir / path_value).resolve()
            if source in seen:
                return
            seen.add(source)
            sources.append(source)

        configured = manifest.get("commonSkillFolders")
        if isinstance(configured, list):
            for item in configured:
                if isinstance(item, dict):
                    add_source(item.get("path"))
                else:
                    add_source(item)

        common_skills_dir = template_dir / "common-skills"
        if common_skills_dir.is_dir():
            for item in sorted(common_skills_dir.iterdir(), key=lambda entry: entry.name):
                if item.is_dir():
                    add_source(str(item.relative_to(template_dir)))
        return sources

    def _ensure_required_libraries(
        self,
        libraries: List[Dict[str, object]],
    ) -> Dict[str, object]:
        results = []
        for library in libraries:
            name = str(library.get("name") or library.get("bin") or "").strip()
            if not name:
                raise ValueError(f"requiredLibraries item missing name: {library}")
            required = library.get("required") is not False
            installed, check_result = self._library_is_installed(library)
            item_result: Dict[str, object] = {
                "name": name,
                "required": required,
                "installed_before": installed,
                "check": check_result,
            }
            if installed:
                item_result["action"] = "continue"
                results.append(item_result)
                continue

            install_command = str(library.get("installCommand") or "").strip()
            if not install_command:
                if required:
                    raise RuntimeError(f"Required library '{name}' is not installed and has no installCommand")
                item_result["action"] = "skipped"
                item_result["reason"] = "not_required_without_install_command"
                results.append(item_result)
                continue

            install_result = self.runner.run(
                ["/bin/sh", "-lc", install_command],
                timeout=self.LIBRARY_INSTALL_TIMEOUT_SECONDS,
            )
            item_result["action"] = "installed"
            item_result["install"] = self._command_result_payload(install_result)
            installed_after, verify_after = self._library_is_installed(library)
            item_result["installed_after"] = installed_after
            item_result["verify_after"] = verify_after
            if not installed_after:
                raise RuntimeError(f"Required library '{name}' install completed but verification still failed")
            results.append(item_result)

        return {
            "library_count": len(libraries),
            "libraries": results,
        }

    def _library_is_installed(self, library: Dict[str, object]) -> tuple[bool, Dict[str, object]]:
        verify_command = str(library.get("verifyCommand") or "").strip()
        bin_name = str(library.get("bin") or "").strip()
        if verify_command:
            command = verify_command
        elif bin_name:
            command = f"command -v {bin_name}"
        else:
            return False, {"skipped": True, "reason": "missing_verify_command_or_bin"}

        try:
            result = self.runner.run(
                ["/bin/sh", "-lc", command],
                timeout=self.LIBRARY_VERIFY_TIMEOUT_SECONDS,
            )
        except CommandError as exc:
            return False, {
                "command": exc.result.command_text,
                "returncode": exc.result.returncode,
                "stdout": exc.result.stdout,
                "stderr": exc.result.stderr,
            }
        return True, self._command_result_payload(result)

    def _install_common_skills(self, sources: List[Path]) -> Dict[str, object]:
        target_root = self.config_path.parent / "skills"
        if self.runner.dry_run:
            return {
                "skipped": True,
                "target_root": str(target_root),
                "skill_count": len(sources),
                "skills": [source.name for source in sources],
            }
        target_root.mkdir(parents=True, exist_ok=True)
        installed = []
        for source in sources:
            if not source.is_dir():
                raise FileNotFoundError(f"Common skill folder not found: {source}")
            destination = target_root / source.name
            shutil.copytree(source, destination, dirs_exist_ok=True)
            installed.append(
                {
                    "name": source.name,
                    "source": str(source),
                    "destination": str(destination),
                }
            )
        return {
            "target_root": str(target_root),
            "skill_count": len(installed),
            "skills": installed,
        }

    def _configure_config_models(
        self,
        model_key: str,
        supported_models: List[Dict[str, object]],
        primary_model: Optional[str] = None,
    ) -> Dict[str, object]:
        config_path = self.config_path
        managed_model_refs = [item["model_ref"] for item in supported_models]
        resolved_primary_model = self._select_primary_model_ref(
            supported_models,
            preferred_model_ref=primary_model,
        )
        if self.runner.dry_run:
            return {
                "skipped": True,
                "config_path": str(config_path),
                "primary_model": resolved_primary_model,
                "managed_models": managed_model_refs,
            }

        config = self._load_config()
        if not isinstance(config, dict):
            raise ValueError(f"Config must be a JSON object: {config_path}")

        agents = config.setdefault("agents", {})
        defaults = agents.setdefault("defaults", {})
        defaults["models"] = {model_ref: {} for model_ref in managed_model_refs}
        defaults["model"] = {"primary": resolved_primary_model}

        config["models"] = {
            "mode": "merge",
            "providers": {
                self.MANAGED_MODEL_PROVIDER: {
                    "baseUrl": "https://unitag.dola.fi/aigateway/v1",
                    "api": "openai-completions",
                    "apiKey": model_key,
                    "models": [item["definition"] for item in supported_models],
                }
            },
        }

        self._write_config(
            config,
            note="configure models for create_instance",
            changed_paths=[
                "agents.defaults.models",
                "agents.defaults.model",
                "models",
            ],
            extra={
                "primary_model": resolved_primary_model,
                "managed_models": managed_model_refs,
            },
        )
        return {
            "config_path": str(config_path),
            "primary_model": resolved_primary_model,
            "managed_models": managed_model_refs,
        }

    def _configure_config_tools(self) -> Dict[str, object]:
        config_path = self.config_path
        if self.runner.dry_run:
            return {
                "skipped": True,
                "config_path": str(config_path),
                "tools_profile": "coding",
                "exec_security": "full",
                "web_search_enabled": False,
                "web_fetch_enabled": True,
            }

        config = self._load_config()
        if not isinstance(config, dict):
            raise ValueError(f"Config must be a JSON object: {config_path}")

        tools = config.setdefault("tools", {})
        tools["profile"] = "coding"
        exec_config = tools.setdefault("exec", {})
        exec_config["security"] = "full"
        web = tools.setdefault("web", {})
        web["search"] = {"enabled": False}
        web["fetch"] = {"enabled": True}

        self._write_config(
            config,
            note="configure tools for create_instance",
            changed_paths=[
                "tools.profile",
                "tools.exec.security",
                "tools.web.search",
                "tools.web.fetch",
            ],
            extra={
                "tools_profile": "coding",
                "exec_security": "full",
                "web_search_enabled": False,
                "web_fetch_enabled": True,
            },
        )
        return {
            "config_path": str(config_path),
            "tools_profile": "coding",
            "exec_security": "full",
            "web_search_enabled": False,
            "web_fetch_enabled": True,
        }

    def _configure_gateway_auth(self, gateway_token: str) -> Dict[str, object]:
        config_path = self.config_path
        if self.runner.dry_run:
            return {
                "skipped": True,
                "config_path": str(config_path),
                "gateway_auth_mode": "token",
                "gateway_token": gateway_token,
            }

        config = self._load_config()
        if not isinstance(config, dict):
            raise ValueError(f"Config must be a JSON object: {config_path}")

        gateway = config.setdefault("gateway", {})
        auth = gateway.setdefault("auth", {})
        auth["mode"] = "token"
        auth["token"] = gateway_token

        self._write_config(
            config,
            note="configure gateway auth for create_instance",
            changed_paths=[
                "gateway.auth.mode",
                "gateway.auth.token",
            ],
            extra={
                "gateway_auth_mode": "token",
            },
        )
        return {
            "config_path": str(config_path),
            "gateway_auth_mode": "token",
            "gateway_token": gateway_token,
        }

    def _run_timed_step(self, steps: List[Dict[str, object]], step: str, func):
        self.runner.log(f"step: start {step}")
        started_at = perf_counter()
        result = func()
        elapsed_ms = round((perf_counter() - started_at) * 1000, 1)
        self.runner.log(f"step: done {step} ({elapsed_ms} ms)")
        steps.append(self._build_step_payload(step, result, elapsed_ms=elapsed_ms))
        return result

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

    def _provision_agent_from_template(
        self,
        *,
        steps: List[Dict[str, object]],
        template_name: str,
        agent_name: str,
        archive_path: Path,
        template_dir: Path,
        workspace: Path,
        model: Optional[str],
        rollback_on_fail: bool,
        step_scope: Optional[str],
    ) -> Dict[str, object]:
        self._ensure_sources_ready(archive_path=archive_path)
        workspace_has_content = self._workspace_has_content(workspace)
        agent_exists = self._agent_exists(agent_name)

        created_agent = False
        started_template_prepare = False
        created_workspace = False

        try:
            started_template_prepare = True
            self._run_timed_step(
                steps,
                self._scoped_step_name("template.prepare", step_scope),
                lambda: self._prepare_template_dir(
                    archive_path=archive_path,
                    template_dir=template_dir,
                ),
            )
            manifest = self._load_template_manifest(template_dir)
            required_libraries = self._required_libraries_from_manifest(manifest)
            if required_libraries:
                self._run_timed_step(
                    steps,
                    self._scoped_step_name("libraries.ensure", step_scope),
                    lambda: self._ensure_required_libraries(required_libraries),
                )

            common_skill_sources = self._common_skill_sources_from_manifest(
                template_dir=template_dir,
                manifest=manifest,
            )
            if common_skill_sources:
                self._run_timed_step(
                    steps,
                    self._scoped_step_name("common_skills.install", step_scope),
                    lambda: self._install_common_skills(common_skill_sources),
                )

            if agent_exists:
                self.runner.log(f"agent exists, skip add: {agent_name}")
                agent_result = {
                    "skipped": True,
                    "reason": "agent_exists",
                    "agent_name": agent_name,
                }
                steps.append(
                    self._build_step_payload(
                        self._scoped_step_name("agents.add", step_scope),
                        agent_result,
                    )
                )
            else:
                self._run_timed_step(
                    steps,
                    self._scoped_step_name("agents.add", step_scope),
                    lambda: self._add_agent(
                        agent_name=agent_name,
                        workspace=workspace,
                        model=model,
                    ),
                )
                created_agent = True

            if workspace_has_content:
                self.runner.log(f"workspace not empty, skip populate: {workspace}")
                workspace_result = {
                    "skipped": True,
                    "reason": "workspace_not_empty",
                    "workspace": str(workspace),
                }
                steps.append(
                    self._build_step_payload(
                        self._scoped_step_name("workspace.populate", step_scope),
                        workspace_result,
                    )
                )
            else:
                workspace_result = self._run_timed_step(
                    steps,
                    self._scoped_step_name("workspace.populate", step_scope),
                    lambda: self._populate_workspace(
                        template_dir=template_dir,
                        workspace=workspace,
                    ),
                )
                created_workspace = not workspace_result.get("skipped", False)
            return {
                "created_agent": created_agent,
                "started_template_prepare": started_template_prepare,
                "created_workspace": created_workspace,
            }
        except Exception as exc:
            rollback_steps: List[Dict[str, object]] = []
            if rollback_on_fail:
                if created_workspace or (created_agent and workspace.exists()):
                    rollback_steps.append(self._safe_purge_workspace(workspace))
                if created_agent:
                    rollback_steps.append(self._safe_delete_agent(agent_name))
                if started_template_prepare and template_dir.exists():
                    rollback_steps.append(self._safe_purge_template_dir(template_dir))
            raise RuntimeError(
                json.dumps(
                    {
                        "error": str(exc),
                        "details": self._error_details(exc),
                        "context": {
                            "template_name": template_name,
                            "agent_name": agent_name,
                            "workspace": str(workspace),
                            "archive_path": str(archive_path),
                            "template_dir": str(template_dir),
                        },
                        "steps": steps,
                        "rollback": rollback_steps,
                    },
                    ensure_ascii=False,
                )
            ) from exc

    def _embedded_error_payload(self, exc: Exception) -> Dict[str, object]:
        try:
            payload = json.loads(str(exc))
        except (TypeError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _resolve_unpacked_root(self, extract_dir: Path) -> Path:
        candidates = [item for item in extract_dir.iterdir() if item.name != "__MACOSX"]
        directories = [item for item in candidates if item.is_dir()]
        files = [item for item in candidates if item.is_file()]
        if len(directories) == 1 and not files:
            return directories[0]
        return extract_dir

    def _copy_directory_contents(self, source_dir: Path, target_dir: Path) -> List[str]:
        copied: List[str] = []
        for item in sorted(source_dir.iterdir(), key=lambda entry: entry.name):
            destination = target_dir / item.name
            if item.is_dir():
                shutil.copytree(item, destination, dirs_exist_ok=True)
            else:
                target_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, destination)
            copied.append(item.name)
        return copied

    def _safe_delete_agent(self, agent_name: str) -> Dict[str, object]:
        try:
            result = self.runner.run_json([self.bin, "agents", "delete", agent_name, "--force", "--json"])
            return {"step": "rollback.agents.delete", "result": result}
        except Exception as exc:
            return {"step": "rollback.agents.delete", "error": str(exc)}

    def _safe_purge_workspace(self, workspace: Path) -> Dict[str, object]:
        try:
            if not workspace.exists():
                return {"step": "rollback.workspace.purge", "result": {"deleted": False, "path": str(workspace)}}
            shutil.rmtree(workspace)
            return {"step": "rollback.workspace.purge", "result": {"deleted": True, "path": str(workspace)}}
        except Exception as exc:
            return {"step": "rollback.workspace.purge", "error": str(exc)}

    def _safe_purge_template_dir(self, template_dir: Path) -> Dict[str, object]:
        try:
            if not template_dir.exists():
                return {"step": "rollback.template.purge", "result": {"deleted": False, "path": str(template_dir)}}
            shutil.rmtree(template_dir)
            return {"step": "rollback.template.purge", "result": {"deleted": True, "path": str(template_dir)}}
        except Exception as exc:
            return {"step": "rollback.template.purge", "error": str(exc)}

    def _error_details(self, exc: Exception) -> Dict[str, object]:
        if isinstance(exc, CommandError):
            return {
                "command": exc.result.command_text,
                "returncode": exc.result.returncode,
                "stdout": exc.result.stdout,
                "stderr": exc.result.stderr,
            }
        return {}

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

    def _generate_tg_bot_name(self) -> str:
        return f"tgbot-{uuid.uuid4().hex[:8]}"

    def _generate_gateway_token(self) -> str:
        return secrets.token_urlsafe(32)

    def _fetch_supported_gateway_models(self) -> Dict[str, object]:
        self.runner.log(f"models: fetch catalog {self.MODEL_CATALOG_URL}")
        request = Request(
            self.MODEL_CATALOG_URL,
            headers={
                "Accept": "application/json",
                "User-Agent": "agent_manage/1.0",
            },
        )
        try:
            with urlopen(request, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except URLError as exc:
            raise RuntimeError(f"Failed to fetch model catalog: {exc}") from exc

        content = payload.get("content")
        if not isinstance(content, list):
            raise ValueError("Model catalog response missing 'content' list")

        models = [
            self._normalize_catalog_model(item)
            for item in content
            if isinstance(item, dict) and item.get("isActive") is True
        ]
        if not models:
            raise ValueError("Model catalog did not contain any active models")

        models.sort(key=self._supported_model_sort_key)
        return {
            "source_url": self.MODEL_CATALOG_URL,
            "model_count": len(models),
            "models": models,
            "primary_model": self._select_primary_model_ref(models),
        }

    def _normalize_catalog_model(self, item: Dict[str, object]) -> Dict[str, object]:
        model_id = str(item.get("identifier") or "").strip()
        if not model_id:
            raise ValueError(f"Model catalog entry missing identifier: {item}")

        context_window = self._to_int(item.get("contextWindowTokens"))
        if context_window is None:
            raise ValueError(f"Model catalog entry missing contextWindowTokens for '{model_id}'")

        display_name = str(item.get("displayName") or model_id).strip()
        return {
            "id": model_id,
            "model_ref": f"{self.MANAGED_MODEL_PROVIDER}/{model_id}",
            "definition": {
                "id": model_id,
                "name": display_name,
                "contextWindow": context_window,
                "maxTokens": self.DEFAULT_MODEL_MAX_TOKENS,
                "input": ["text"],
                "cost": {
                    "input": self._to_number(item.get("inputTokenPrice")),
                    "output": self._to_number(item.get("outputTokenPrice")),
                    "cacheRead": self._to_number(item.get("cachedInputTokenPrice")),
                    "cacheWrite": 0,
                },
                "reasoning": item.get("reasoningTokenPrice") is not None,
            },
            "upstream": {
                "display_name": display_name,
                "model_provider_identifier": item.get("modelProviderIdentifier"),
                "model_provider_display_name": item.get("modelProviderDisplayName"),
                "currency": item.get("currency"),
                "token_pricing_unit": item.get("tokenPricingUnit"),
                "reasoning_token_price": self._to_number(item.get("reasoningTokenPrice")),
            },
        }

    def _select_primary_model_ref(
        self,
        supported_models: List[Dict[str, object]],
        preferred_model_ref: Optional[str] = None,
    ) -> str:
        supported_refs = {item["model_ref"] for item in supported_models}
        if preferred_model_ref and preferred_model_ref in supported_refs:
            return preferred_model_ref
        by_id = {item["id"]: item["model_ref"] for item in supported_models}
        for model_id in self.PREFERRED_PRIMARY_MODEL_IDS:
            if model_id in by_id:
                return by_id[model_id]
        return supported_models[0]["model_ref"]

    def _supported_model_sort_key(self, item: Dict[str, object]) -> tuple[int, str]:
        model_id = item["id"]
        try:
            index = self.PREFERRED_PRIMARY_MODEL_IDS.index(model_id)
        except ValueError:
            index = len(self.PREFERRED_PRIMARY_MODEL_IDS)
        return (index, model_id)

    def _supported_models_from_config(self, config: Dict[str, object]) -> List[Dict[str, object]]:
        provider = config.get("models", {}).get("providers", {}).get(self.MANAGED_MODEL_PROVIDER, {})
        definitions = provider.get("models", []) if isinstance(provider, dict) else []
        models: List[Dict[str, object]] = []
        for item in definitions:
            if not isinstance(item, dict):
                continue
            model_id = item.get("id")
            if not isinstance(model_id, str) or not model_id.strip():
                continue
            model_id = model_id.strip()
            models.append(
                {
                    "id": model_id,
                    "model_ref": f"{self.MANAGED_MODEL_PROVIDER}/{model_id}",
                    "definition": item,
                }
            )
        models.sort(key=self._supported_model_sort_key)
        return models

    def _supported_model_refs_from_config(self) -> List[str]:
        config = self._load_config()
        supported_refs = [item["model_ref"] for item in self._supported_models_from_config(config)]
        if not supported_refs:
            raise ValueError(f"No supported models configured under provider '{self.MANAGED_MODEL_PROVIDER}'")
        return supported_refs

    def _configured_default_model_from_config(self, config: Dict[str, object]) -> Optional[str]:
        defaults = config.get("agents", {}).get("defaults", {})
        model = defaults.get("model", {}) if isinstance(defaults, dict) else {}
        if isinstance(model, dict):
            primary = model.get("primary")
            if isinstance(primary, str) and primary.strip():
                return primary.strip()
        return None

    def _configured_model_api_key_from_config(self, config: Dict[str, object]) -> str:
        provider = config.get("models", {}).get("providers", {}).get(self.MANAGED_MODEL_PROVIDER, {})
        api_key = provider.get("apiKey") if isinstance(provider, dict) else None
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError(f"Configured apiKey not found for provider '{self.MANAGED_MODEL_PROVIDER}'")
        return api_key.strip()

    def _restart_gateway_service(self) -> Dict[str, object]:
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
        needle = f":{self.GATEWAY_PORT}"
        return any(needle in line for line in result.stdout.splitlines())

    def _to_int(self, value) -> Optional[int]:
        if value is None:
            return None
        return int(value)

    def _to_number(self, value) -> float:
        if value is None:
            return 0
        return float(value)

    def _normalize_weixin_account_id(self, account_id: str) -> str:
        normalized = []
        last_dash = False
        for char in account_id.strip().lower():
            if char.isalnum():
                normalized.append(char)
                last_dash = False
                continue
            if char in {"-", "_"}:
                normalized.append(char)
                last_dash = False
                continue
            if not last_dash:
                normalized.append("-")
                last_dash = True
        value = "".join(normalized).strip("-")
        if not value:
            raise ValueError("Invalid Weixin account id")
        return value

    def _prepare_weixin_plugin_config(self) -> Dict[str, object]:
        steps: List[Dict[str, object]] = []

        config = self._load_config()
        plugin_entry = (
            config.setdefault("plugins", {})
            .setdefault("entries", {})
            .setdefault(self.WEIXIN_PLUGIN_ID, {})
        )
        config_updated = plugin_entry.get("enabled") is not True
        if config_updated:
            plugin_entry["enabled"] = True
            self._write_config(
                config,
                note=f"enable plugin {self.WEIXIN_PLUGIN_ID}",
                changed_paths=[f"plugins.entries.{self.WEIXIN_PLUGIN_ID}.enabled"],
            )

        return {
            "plugin_id": self.WEIXIN_PLUGIN_ID,
            "install_check_skipped": True,
            "enabled": True,
            "config_updated": config_updated,
            "restart_required": True,
            "steps": steps,
        }

    def _write_weixin_account_state(
        self,
        account_id: str,
        bot_token: str,
        base_url: str,
        user_id: Optional[str],
    ) -> Dict[str, object]:
        state_root = self.config_path.parent / self.WEIXIN_PLUGIN_ID
        accounts_dir = state_root / "accounts"
        accounts_dir.mkdir(parents=True, exist_ok=True)
        account_path = accounts_dir / f"{account_id}.json"
        index_path = state_root / "accounts.json"

        payload = {
            "token": bot_token,
            "savedAt": datetime.now().isoformat(),
            "baseUrl": base_url,
        }
        if user_id:
            payload["userId"] = user_id
        account_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        try:
            account_path.chmod(0o600)
        except OSError:
            pass

        existing_ids: List[str] = []
        if index_path.exists():
            try:
                parsed = json.loads(index_path.read_text(encoding="utf-8"))
                if isinstance(parsed, list):
                    existing_ids = [item for item in parsed if isinstance(item, str)]
            except json.JSONDecodeError:
                existing_ids = []
        if account_id not in existing_ids:
            existing_ids.append(account_id)
        index_path.write_text(
            json.dumps(existing_ids, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        return {
            "state_dir": str(state_root),
            "account_path": str(account_path),
            "index_path": str(index_path),
        }

    def _load_weixin_account_state(self, account_id: str) -> Optional[Dict[str, object]]:
        account_path = self._weixin_accounts_dir() / f"{account_id}.json"
        if not account_path.exists():
            return None
        try:
            payload = json.loads(account_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def _delete_weixin_account_state(self, account_id: str) -> Dict[str, object]:
        accounts_dir = self._weixin_accounts_dir()
        state_root = self.config_path.parent / self.WEIXIN_PLUGIN_ID
        index_path = state_root / "accounts.json"
        deleted_files: List[str] = []
        for suffix in (".json", ".sync.json", ".context-tokens.json"):
            target = accounts_dir / f"{account_id}{suffix}"
            if target.exists():
                try:
                    target.unlink()
                except FileNotFoundError:
                    continue
                deleted_files.append(str(target))

        remaining_ids: List[str] = []
        if index_path.exists():
            try:
                parsed = json.loads(index_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                parsed = []
            if isinstance(parsed, list):
                remaining_ids = [item for item in parsed if isinstance(item, str) and item != account_id]
                index_path.write_text(
                    json.dumps(remaining_ids, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
        return {
            "deleted_files": deleted_files,
            "index_path": str(index_path),
            "remaining_index_count": len(remaining_ids),
        }

    def _clear_stale_weixin_accounts_for_user(
        self,
        current_account_id: str,
        user_id: Optional[str],
    ) -> List[str]:
        value = (user_id or "").strip()
        if not value:
            return []
        state_root = self.config_path.parent / self.WEIXIN_PLUGIN_ID
        accounts_dir = self._weixin_accounts_dir()
        index_path = state_root / "accounts.json"
        if not accounts_dir.exists():
            return []

        removed: List[str] = []
        for account_path in sorted(accounts_dir.glob("*.json")):
            if account_path.name.endswith(".sync.json") or account_path.name.endswith(".context-tokens.json"):
                continue
            account_id = account_path.stem
            if account_id == current_account_id:
                continue
            try:
                payload = json.loads(account_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            if (payload.get("userId") or "").strip() != value:
                continue
            removed.append(account_id)
            for suffix in (".json", ".sync.json", ".context-tokens.json"):
                target = accounts_dir / f"{account_id}{suffix}"
                if target.exists():
                    try:
                        target.unlink()
                    except FileNotFoundError:
                        continue

        if removed and index_path.exists():
            try:
                parsed = json.loads(index_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                parsed = []
            if isinstance(parsed, list):
                updated = [item for item in parsed if item not in removed]
                index_path.write_text(
                    json.dumps(updated, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
        return removed

    def _channel_timestamp(self) -> str:
        return datetime.now().isoformat()

    def _weixin_accounts_dir(self) -> Path:
        return self.config_path.parent / self.WEIXIN_PLUGIN_ID / "accounts"

    def _load_config(self) -> Dict[str, object]:
        self.runner.log(f"config: load {self.config_path}")
        if not self.config_path.exists():
            raise FileNotFoundError(f"Config file not found: {self.config_path}")
        return json.loads(self.config_path.read_text(encoding="utf-8"))

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
        if self.config_path.exists():
            shutil.copy2(self.config_path, backup_path)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(self.config_path.parent),
            delete=False,
        ) as handle:
            json.dump(config, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            tmp_path = Path(handle.name)
        tmp_path.replace(self.config_path)
        payload["backup_path"] = str(backup_path) if backup_path.exists() else None
        return payload
