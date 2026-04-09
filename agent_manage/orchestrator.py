from __future__ import annotations

import json
import shutil
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .local import CommandError, LocalRunner

from .models import (
    AddTelegramBotRequest,
    AddWeixinBotRequest,
    CreateInstanceRequest,
    DeleteTelegramBotRequest,
    DeleteWeixinBotRequest,
    MANAGED_MODEL_IDS,
    SUPPORTED_MODEL_REFS,
    SetModelRequest,
)


class InstanceManagerV2:
    SERVER_STATUS_TIMEOUT_SECONDS = 10
    WEIXIN_PLUGIN_ID = "openclaw-weixin"
    WEIXIN_PLUGIN_PACKAGE = "@tencent-weixin/openclaw-weixin"
    WEIXIN_DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
    MANAGED_MODEL_PROVIDER = "unipay-fun"

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

        self._ensure_sources_ready(archive_path=archive_path)
        workspace_has_content = self._workspace_has_content(workspace)
        agent_exists = self._agent_exists(agent_name)

        created_agent = False
        started_template_prepare = False
        created_workspace = False

        try:
            started_template_prepare = True
            template_result = self._prepare_template_dir(
                archive_path=archive_path,
                template_dir=template_dir,
            )
            steps.append({"step": "template.prepare", "result": template_result})

            if agent_exists:
                self.runner.log(f"agent exists, skip add: {agent_name}")
                agent_result = {
                    "skipped": True,
                    "reason": "agent_exists",
                    "agent_name": agent_name,
                }
            else:
                agent_result = self._add_agent(
                    agent_name=agent_name,
                    workspace=workspace,
                    model=request.model,
                )
                created_agent = True
            steps.append({"step": "agents.add", "result": agent_result})

            if workspace_has_content:
                self.runner.log(f"workspace not empty, skip populate: {workspace}")
                workspace_result = {
                    "skipped": True,
                    "reason": "workspace_not_empty",
                    "workspace": str(workspace),
                }
            else:
                workspace_result = self._populate_workspace(
                    template_dir=template_dir,
                    workspace=workspace,
                )
                created_workspace = not workspace_result.get("skipped", False)
            steps.append({"step": "workspace.populate", "result": workspace_result})

            models_result = self._configure_config_models(
                model_key=request.model_key,
            )
            steps.append({"step": "config.configure_models", "result": models_result})

            tools_result = self._configure_config_tools()
            steps.append({"step": "config.configure_tools", "result": tools_result})

            return {
                "ok": True,
                "template_name": request.template_name,
                "agent_name": agent_name,
                "workspace": str(workspace),
                "archive_path": str(archive_path),
                "template_dir": str(template_dir) if template_dir else None,
                "steps": steps,
            }
        except Exception as exc:
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
        restart_result = self.runner.run(
            [self.bin, "gateway", "restart"],
            stream_output=True,
        )
        return {
            "ok": True,
            "agent_name": request.agent_name,
            "bot_name": account_id,
            "config_write": write_result,
            "gateway_restart": self._command_step("gateway.restart", restart_result),
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

        restart_result = self.runner.run(
            [self.bin, "gateway", "restart"],
            stream_output=True,
        )
        plugin_prepare.setdefault("steps", []).append(
            self._command_step("gateway.restart", restart_result)
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
        if request.model_ref not in SUPPORTED_MODEL_REFS:
            allowed = ", ".join(sorted(SUPPORTED_MODEL_REFS))
            raise ValueError(f"Unsupported model '{request.model_ref}'. Allowed: {allowed}")

        set_result = self.runner.run([self.bin, "models", "set", request.model_ref])

        return {
            "ok": True,
            "model_ref": request.model_ref,
            "steps": [
                self._command_step("models.set", set_result),
            ],
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
        return self.runner.run_json(args)

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

    def _configure_config_models(self, model_key: str) -> Dict[str, object]:
        config_path = self.config_path
        if self.runner.dry_run:
            return {
                "skipped": True,
                "config_path": str(config_path),
                "primary_model": self._managed_primary_model_ref(),
                "managed_models": self._managed_model_refs(),
            }

        config = self._load_config()
        if not isinstance(config, dict):
            raise ValueError(f"Config must be a JSON object: {config_path}")

        agents = config.setdefault("agents", {})
        defaults = agents.setdefault("defaults", {})
        defaults["models"] = {model_ref: {} for model_ref in self._managed_model_refs()}
        defaults["model"] = {"primary": self._managed_primary_model_ref()}

        config["models"] = {
            "mode": "merge",
            "providers": {
                self.MANAGED_MODEL_PROVIDER: {
                    "baseUrl": "https://unitag.dola.fi/aigateway/v1",
                    "api": "openai-completions",
                    "apiKey": model_key,
                    "models": [self._managed_model_definition(model_id) for model_id in MANAGED_MODEL_IDS],
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
                "primary_model": self._managed_primary_model_ref(),
                "managed_models": self._managed_model_refs(),
            },
        )
        return {
            "config_path": str(config_path),
            "primary_model": self._managed_primary_model_ref(),
            "managed_models": self._managed_model_refs(),
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
        payload = {
            "command": result.command_text,
            "returncode": result.returncode,
            "skipped": result.skipped,
        }
        if result.stdout.strip():
            payload["stdout"] = result.stdout
        if result.stderr.strip():
            payload["stderr"] = result.stderr
        return {"step": step, "result": payload}

    def _generate_tg_bot_name(self) -> str:
        return f"tgbot-{uuid.uuid4().hex[:8]}"

    def _managed_primary_model_ref(self) -> str:
        return f"{self.MANAGED_MODEL_PROVIDER}/{MANAGED_MODEL_IDS[0]}"

    def _managed_model_refs(self) -> List[str]:
        return [f"{self.MANAGED_MODEL_PROVIDER}/{model_id}" for model_id in MANAGED_MODEL_IDS]

    def _managed_model_definition(self, model_id: str) -> Dict[str, object]:
        model_specs = {
            "gpt-5.4-nano": {"contextWindow": 400000, "maxTokens": 128000},
            "gpt-5.4": {"contextWindow": 1050000, "maxTokens": 128000},
            "gpt-5.3-codex": {"contextWindow": 400000, "maxTokens": 128000},
            "gpt-5.4-mini": {"contextWindow": 400000, "maxTokens": 128000},
            "gpt-5-nano": {"contextWindow": 400000, "maxTokens": 128000},
        }
        try:
            spec = model_specs[model_id]
        except KeyError as exc:
            raise ValueError(f"Unsupported managed model definition for '{model_id}'") from exc

        return {
            "id": model_id,
            "name": f"{model_id} (Custom Provider)",
            "contextWindow": spec["contextWindow"],
            "maxTokens": spec["maxTokens"],
            "input": ["text"],
            "cost": {
                "input": 0,
                "output": 0,
                "cacheRead": 0,
                "cacheWrite": 0,
            },
            "reasoning": True,
        }

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
