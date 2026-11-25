"""Telegram, Feishu, and Weixin channel configuration operations."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .channel_bindings import count_channel_bindings, remove_account_bindings, replace_account_binding

from .models import (
    AddFeishuBotRequest,
    AddTelegramBotRequest,
    AddWeixinBotRequest,
    DeleteFeishuBotRequest,
    DeleteTelegramBotRequest,
    DeleteWeixinBotRequest,
)


class ChannelManagementMixin:
    """Manage channel accounts, bindings, and persisted channel state."""

    def add_tg_bot(self, request: AddTelegramBotRequest) -> Dict[str, object]:
        self._ensure_agent_exists_in_config(request.agent_name)
        bot_token = request.bot_token.strip()
        if not bot_token:
            raise ValueError("bot_token is required")

        account_id = (request.bot_name or "").strip() or self._generate_tg_bot_name()
        config = self._load_config()
        telegram = config.setdefault("channels", {}).setdefault("telegram", {})
        telegram["enabled"] = True
        accounts = telegram.setdefault("accounts", {})
        accounts[account_id] = {
            "botToken": bot_token,
            "dmPolicy": "open",
            "allowFrom": ["*"],
        }

        removed = replace_account_binding(config, "telegram", account_id, request.agent_name)

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

    def add_feishu_bot(self, request: AddFeishuBotRequest) -> Dict[str, object]:
        self._ensure_agent_exists_in_config(request.agent_name)

        account_id = self._normalize_feishu_account_id(request.account_id)
        domain = self._normalize_feishu_domain(request.domain)
        app_id = request.app_id.strip()
        app_secret = request.app_secret.strip()
        dm_policy = request.dm_policy.strip() or "open"
        allow_from = self._normalize_allow_from(request.allow_from)
        if not app_id:
            raise ValueError("app_id is required")
        if not app_secret:
            raise ValueError("app_secret is required")

        config = self._load_config()
        feishu = config.setdefault("channels", {}).setdefault(self.FEISHU_CHANNEL_ID, {})
        feishu["enabled"] = True
        feishu["domain"] = domain
        feishu["dmPolicy"] = dm_policy
        feishu["allowFrom"] = allow_from
        feishu.setdefault("defaultAccount", account_id)
        accounts = feishu.setdefault("accounts", {})
        account_config = dict(accounts.get(account_id, {}))
        account_config.update(
            {
                "domain": domain,
                "appId": app_id,
                "appSecret": app_secret,
                "enabled": True,
            }
        )
        if request.bot_name:
            account_config["name"] = request.bot_name
        accounts[account_id] = account_config

        removed = replace_account_binding(config, self.FEISHU_CHANNEL_ID, account_id, request.agent_name)

        write_result = self._write_config(
            config,
            note=f"add feishu bot {account_id} for agent {request.agent_name}",
            changed_paths=[
                f"channels.{self.FEISHU_CHANNEL_ID}.enabled",
                f"channels.{self.FEISHU_CHANNEL_ID}.domain",
                f"channels.{self.FEISHU_CHANNEL_ID}.dmPolicy",
                f"channels.{self.FEISHU_CHANNEL_ID}.allowFrom",
                f"channels.{self.FEISHU_CHANNEL_ID}.accounts.{account_id}",
                "bindings",
            ],
            extra={
                "binding_agent": request.agent_name,
                "removed_existing_bindings": removed,
            },
        )

        bind_result = None
        if request.bind_lark_cli:
            bind_result = self._bind_lark_cli_feishu_app(
                app_id=app_id,
                identity=request.lark_cli_identity,
            )

        restart_result = self._restart_gateway_service()
        return {
            "ok": True,
            "agent_name": request.agent_name,
            "account_id": account_id,
            "domain": domain,
            "app_id": app_id,
            "bot_name": request.bot_name,
            "dm_policy": dm_policy,
            "allow_from": allow_from,
            "config_write": write_result,
            "lark_cli_bind": bind_result,
            "gateway_restart": self._build_step_payload("gateway.restart", restart_result),
        }

    def add_weixin_bot(self, request: AddWeixinBotRequest) -> Dict[str, object]:
        self._ensure_agent_exists_in_config(request.agent_name)
        bot_token = request.bot_token.strip()
        if not bot_token:
            raise ValueError("bot_token is required")
        normalized_account_id = self._normalize_weixin_account_id(request.ilink_bot_id)
        plugin_prepare = self._prepare_weixin_plugin_config()
        stale_accounts = self._clear_stale_weixin_accounts_for_user(
            current_account_id=normalized_account_id,
            user_id=request.ilink_user_id,
        )
        state_result = self._write_weixin_account_state(
            account_id=normalized_account_id,
            bot_token=bot_token,
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

        removed = replace_account_binding(config, self.WEIXIN_PLUGIN_ID, normalized_account_id, request.agent_name)

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

    def get_tg_bot_status(self) -> Dict[str, object]:
        config = self._load_config()
        telegram = config.get("channels", {}).get("telegram", {})
        accounts = telegram.get("accounts", {})
        binding_counts = count_channel_bindings(config, "telegram")

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

    def get_feishu_bot_status(self) -> Dict[str, object]:
        config = self._load_config()
        feishu = config.get("channels", {}).get(self.FEISHU_CHANNEL_ID, {})
        accounts = feishu.get("accounts", {})
        binding_counts = count_channel_bindings(config, self.FEISHU_CHANNEL_ID)

        bots: List[Dict[str, object]] = []
        for account_id in sorted(accounts.keys()):
            account = accounts.get(account_id, {})
            bound_count = binding_counts.get(account_id, 0)
            app_id = account.get("appId")
            bots.append(
                {
                    "account_id": account_id,
                    "domain": account.get("domain", "feishu"),
                    "app_id": app_id,
                    "app_id_masked": self._mask_app_id(app_id),
                    "bot_name": account.get("name"),
                    "enabled": bool(account.get("enabled", feishu.get("enabled", False))),
                    "binding_count": bound_count,
                    "is_bound": bound_count > 0,
                    "dm_policy": feishu.get("dmPolicy"),
                    "allow_from": feishu.get("allowFrom"),
                    "has_app_secret": bool(account.get("appSecret")),
                    "lark_cli_bound": self._is_lark_cli_app_bound(app_id),
                }
            )

        return {
            "ok": True,
            "feishu_enabled": bool(feishu.get("enabled", False)),
            "feishu_bot_count": len(accounts),
            "bound_feishu_bot_count": sum(1 for item in bots if item["is_bound"]),
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
        removed_bindings = remove_account_bindings(config, "telegram", request.bot_name)
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

    def delete_feishu_bot(self, request: DeleteFeishuBotRequest) -> Dict[str, object]:
        account_id = self._normalize_feishu_account_id(request.account_id)

        config = self._load_config()
        feishu = config.setdefault("channels", {}).setdefault(self.FEISHU_CHANNEL_ID, {})
        accounts = feishu.setdefault("accounts", {})
        if account_id not in accounts:
            raise FileNotFoundError(f"Feishu account '{account_id}' not found")
        del accounts[account_id]

        removed_bindings = remove_account_bindings(config, self.FEISHU_CHANNEL_ID, account_id)
        feishu["enabled"] = bool(accounts)

        write_result = self._write_config(
            config,
            note=f"delete feishu bot {account_id}",
            changed_paths=[
                f"channels.{self.FEISHU_CHANNEL_ID}.accounts.{account_id}",
                "bindings",
                f"channels.{self.FEISHU_CHANNEL_ID}.enabled",
            ],
            extra={
                "removed_bindings": removed_bindings,
                "remaining_feishu_bot_count": len(accounts),
            },
        )
        restart_result = self._restart_gateway_service()

        return {
            "ok": True,
            "deleted_account_id": account_id,
            "removed_bindings": removed_bindings,
            "remaining_feishu_bot_count": len(accounts),
            "config_write": write_result,
            "gateway_restart": self._build_step_payload("gateway.restart", restart_result),
        }

    def get_weixin_bot_status(self) -> Dict[str, object]:
        config = self._load_config()
        weixin = config.get("channels", {}).get(self.WEIXIN_PLUGIN_ID, {})
        accounts = weixin.get("accounts", {})
        binding_counts = count_channel_bindings(config, self.WEIXIN_PLUGIN_ID)

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
        removed_bindings = remove_account_bindings(config, self.WEIXIN_PLUGIN_ID, normalized_account_id)
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

    def _generate_tg_bot_name(self) -> str:
        return f"tgbot-{uuid.uuid4().hex[:8]}"

    def _normalize_feishu_account_id(self, account_id: str) -> str:
        value = account_id.strip()
        if not value:
            raise ValueError("account_id is required")
        normalized = []
        last_dash = False
        for char in value.lower():
            if char.isalnum():
                normalized.append(char)
                last_dash = False
                continue
            if not last_dash:
                normalized.append("-")
                last_dash = True
        result = "".join(normalized).strip("-")
        if not result:
            raise ValueError("Invalid Feishu account id")
        return result

    def _normalize_feishu_domain(self, domain: str) -> str:
        value = domain.strip().lower()
        if value not in self.FEISHU_DOMAINS:
            raise ValueError("domain must be feishu or lark")
        return value

    def _normalize_allow_from(self, allow_from: Optional[List[str]]) -> List[str]:
        if not allow_from:
            return ["*"]
        values: List[str] = []
        for raw in allow_from:
            for item in raw.split(","):
                value = item.strip()
                if value and value not in values:
                    values.append(value)
        return values or ["*"]

    def _bind_lark_cli_feishu_app(self, app_id: str, identity: str) -> Dict[str, object]:
        if identity not in {"bot-only", "user-default"}:
            raise ValueError("lark_cli_identity must be bot-only or user-default")
        result = self.runner.run(
            [
                "lark-cli",
                "config",
                "bind",
                "--source",
                "openclaw",
                "--app-id",
                app_id,
                "--identity",
                identity,
            ],
            timeout=self.SERVER_STATUS_TIMEOUT_SECONDS,
        )
        return self._command_step("lark-cli.config.bind", result)

    def _mask_app_id(self, app_id: object) -> Optional[str]:
        if not isinstance(app_id, str) or not app_id:
            return None
        if len(app_id) <= 10:
            return app_id
        return f"{app_id[:7]}****{app_id[-4:]}"

    def _is_lark_cli_app_bound(self, app_id: object) -> bool:
        if not isinstance(app_id, str) or not app_id:
            return False
        config_path = Path.home() / ".lark-cli" / "config.json"
        if not config_path.exists():
            return False
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if config.get("appId") == app_id:
            return True
        apps = config.get("apps")
        if isinstance(apps, dict):
            return any(
                isinstance(value, dict) and value.get("appId") == app_id
                for value in apps.values()
            )
        return False

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
