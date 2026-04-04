from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

from .local import CommandError, LocalRunner
from .models import (
    CreateAgentRequest,
    DeleteAgentRequest,
    TelegramAccountConfig,
)


class OpenClawManager:
    def __init__(self, runner: LocalRunner, config_path: Optional[str] = None) -> None:
        self.runner = runner
        self.bin = runner.openclaw_bin
        self.config_path = (
            Path(config_path).expanduser().resolve()
            if config_path
            else Path.home() / ".openclaw" / "openclaw.json"
        )

    def list_models(self):
        return self.runner.run_json([self.bin, "models", "list", "--json"])

    def list_agents(self):
        return self.runner.run_json([self.bin, "agents", "list", "--bindings", "--json"])

    def create(self, request: CreateAgentRequest) -> Dict[str, object]:
        steps: List[Dict[str, object]] = []
        agent_id = self.resolve_agent_id(request.tg_id, request.agent_name)
        workspace = self.default_workspace(agent_id)
        config = self._load_config()
        existing_agent = self._find_agent_optional(config, agent_id)
        selected_account = self._resolve_telegram_account(config, request.telegram)
        created_agent = existing_agent is None
        updated_model = False

        try:
            if existing_agent is None:
                add_args = [
                    self.bin,
                    "agents",
                    "add",
                    agent_id,
                    "--workspace",
                    workspace,
                    "--non-interactive",
                    "--json",
                ]
                if request.model:
                    add_args.extend(["--model", request.model])
                if request.agent_dir:
                    add_args.extend(["--agent-dir", request.agent_dir])
                add_result = self.runner.run_json(add_args)
                steps.append({"step": "agents.add", "result": add_result})
                config = self._load_config()
            else:
                steps.append(
                    {
                        "step": "agents.add",
                        "result": {
                            "skipped": True,
                            "reason": f"agent '{agent_id}' already exists",
                        },
                    }
                )

            if existing_agent is not None and request.model and existing_agent.get("model") != request.model:
                model_result = self._set_agent_model_in_config(config=config, agent_id=agent_id, model=request.model)
                updated_model = True
                steps.append({"step": "model.set", "result": model_result})

            existing_account = self._get_existing_telegram_account(config, selected_account.account_id)
            if request.telegram and request.telegram.bot_token:
                tg_result = self.tg_set(
                    selected_account,
                    tg_id=request.tg_id,
                    existing_account=existing_account,
                )
                steps.append({"step": "tg.set", "result": tg_result})
                config = self._load_config() if not self.runner.dry_run else config
            elif existing_account is not None:
                tg_result = self.tg_set(
                    selected_account,
                    tg_id=request.tg_id,
                    existing_account=existing_account,
                )
                steps.append({"step": "tg.set", "result": tg_result})
                config = self._load_config() if not self.runner.dry_run else config
            else:
                steps.append(
                    {
                        "step": "tg.set",
                        "result": {
                            "skipped": True,
                            "reason": f"telegram account '{selected_account.account_id}' already exists",
                        },
                    }
                )

            bind_result = self._upsert_dm_binding(
                config=config,
                agent_id=agent_id,
                account_id=selected_account.account_id,
                tg_id=request.tg_id,
            )
            steps.append({"step": "bindings.set", "result": bind_result})

            return {
                "ok": True,
                "agent_id": agent_id,
                "tg_id": request.tg_id,
                "workspace": workspace,
                "telegram_account_id": selected_account.account_id,
                "steps": steps,
            }
        except Exception as exc:
            rollback_steps: List[Dict[str, object]] = []
            if request.rollback_on_fail:
                rollback_steps.append(self._safe_remove_peer_binding(request.tg_id))
                rollback_steps.append(self._safe_tg_remove_from_all_accounts(request.tg_id))
                if created_agent:
                    rollback_steps.append(self._safe_delete_agent(agent_id))
                if updated_model:
                    rollback_steps.append({"step": "rollback.model.set", "error": "manual review recommended"})
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

    def delete(self, request: DeleteAgentRequest) -> Dict[str, object]:
        steps: List[Dict[str, object]] = []
        agent_id = self.resolve_agent_id(request.tg_id, request.agent_name)
        workspace = None if self.runner.dry_run else self._get_agent_workspace(agent_id)

        binding_result = self._remove_peer_binding(request.tg_id)
        steps.append({"step": "bindings.remove", "result": binding_result})

        whitelist_result = self._remove_tg_id_from_all_accounts(request.tg_id)
        steps.append({"step": "tg.whitelist.remove", "result": whitelist_result})

        delete_args = [self.bin, "agents", "delete", agent_id]
        if request.force:
            delete_args.append("--force")
        delete_args.append("--json")
        delete_result = self.runner.run_json(delete_args)
        steps.append({"step": "agents.delete", "result": delete_result})

        purge_result = None
        if request.purge_workspace and workspace:
            purge_result = self._purge_workspace(workspace)
            steps.append({"step": "workspace.purge", "result": purge_result})
        elif request.purge_workspace and self.runner.dry_run:
            purge_result = {
                "skipped": True,
                "reason": "workspace lookup skipped in dry-run",
            }
            steps.append({"step": "workspace.purge", "result": purge_result})

        return {
            "ok": True,
            "agent_id": agent_id,
            "tg_id": request.tg_id,
            "workspace": workspace,
            "workspace_purged": bool(purge_result),
            "steps": steps,
        }

    def tg_set(
        self,
        account: TelegramAccountConfig,
        tg_id: str,
        existing_account: Optional[Dict[str, object]] = None,
    ) -> Dict[str, object]:
        token = account.bot_token
        if not token and existing_account is not None:
            token = existing_account.get("botToken")
        if not token:
            raise ValueError("--tg-bot-token is required for tg set")
        config = self._load_config()
        telegram = config.setdefault("channels", {}).setdefault("telegram", {})
        telegram["enabled"] = True
        accounts = telegram.setdefault("accounts", {})
        accounts[account.account_id] = self._telegram_patch(
            token=token,
            tg_id=tg_id,
            existing_account=existing_account,
        )
        return self._write_config(
            config,
            note=f"set telegram account {account.account_id}",
            changed_paths=[f"channels.telegram.accounts.{account.account_id}"],
        )

    def tg_remove(self, account_id: str) -> Dict[str, object]:
        config = self._load_config()
        accounts = (
            config.setdefault("channels", {})
            .setdefault("telegram", {})
            .setdefault("accounts", {})
        )
        removed = account_id in accounts
        accounts.pop(account_id, None)
        return self._write_config(
            config,
            note=f"remove telegram account {account_id}",
            changed_paths=[f"channels.telegram.accounts.{account_id}"],
            extra={"removed": removed},
        )

    def tg_show(self, account_id: str) -> Dict[str, object]:
        config = self._get_full_config()
        accounts = (
            config.get("channels", {})
            .get("telegram", {})
            .get("accounts", {})
        )
        if account_id not in accounts:
            raise FileNotFoundError(f"Telegram account '{account_id}' not found")
        return {
            "account_id": account_id,
            "config": accounts[account_id],
        }

    def _get_full_config(self) -> Dict[str, object]:
        return self._load_config()

    def _get_agent_entry(self, agent_id: str) -> Dict[str, object]:
        agents = self.list_agents()
        entries = self._extract_agent_list(agents)
        for item in entries:
            if item.get("id") == agent_id:
                return item
        raise FileNotFoundError(f"Agent '{agent_id}' not found")

    def _find_agent_optional(self, config: Dict[str, object], agent_id: str) -> Optional[Dict[str, object]]:
        agents = list(config.get("agents", {}).get("list", []))
        for item in agents:
            if item.get("id") == agent_id:
                return item
        if self.runner.dry_run:
            return None
        try:
            return self._get_agent_entry(agent_id)
        except FileNotFoundError:
            return None

    def _get_agent_workspace(self, agent_id: str) -> Optional[str]:
        agent = self._get_agent_entry(agent_id)
        workspace = agent.get("workspace")
        return str(workspace) if workspace else None

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

    def _find_agent(
        self,
        agents: List[Dict[str, object]],
        agent_id: str,
    ) -> Tuple[int, Dict[str, object]]:
        for index, item in enumerate(agents):
            if item.get("id") == agent_id:
                return index, item
        raise FileNotFoundError(f"Agent '{agent_id}' not found in config")

    def _telegram_patch(
        self,
        token: str,
        tg_id: str,
        existing_account: Optional[Dict[str, object]] = None,
    ) -> Dict[str, object]:
        allow_from = []
        if existing_account and isinstance(existing_account.get("allowFrom"), list):
            allow_from.extend(str(item) for item in existing_account["allowFrom"])
        if str(tg_id) not in allow_from:
            allow_from.append(str(tg_id))
        return {
            "botToken": token,
            "dmPolicy": "allowlist",
            "allowFrom": allow_from,
        }

    def _resolve_telegram_account(
        self,
        config: Dict[str, object],
        requested: Optional[TelegramAccountConfig],
    ) -> TelegramAccountConfig:
        accounts = (
            config.get("channels", {})
            .get("telegram", {})
            .get("accounts", {})
        )
        if requested and requested.account_id:
            if requested.bot_token or requested.account_id in accounts or self.runner.dry_run:
                return requested
            raise FileNotFoundError(
                f"telegram account '{requested.account_id}' not found; provide --tg-bot-token to create it"
            )
        names = sorted(accounts.keys())
        if len(names) == 1:
            return TelegramAccountConfig(account_id=names[0], bot_token=None)
        if len(names) == 0:
            raise ValueError("No telegram account configured. Provide --tg-bot and --tg-bot-token.")
        raise ValueError("Multiple telegram accounts exist. Provide --tg-bot explicitly.")

    def _get_existing_telegram_account(
        self,
        config: Dict[str, object],
        account_id: str,
    ) -> Optional[Dict[str, object]]:
        accounts = (
            config.get("channels", {})
            .get("telegram", {})
            .get("accounts", {})
        )
        value = accounts.get(account_id)
        return value if isinstance(value, dict) else None

    def _upsert_dm_binding(
        self,
        config: Dict[str, object],
        agent_id: str,
        account_id: str,
        tg_id: str,
    ) -> Dict[str, object]:
        bindings = list(config.get("bindings", []))
        target = {
            "agentId": agent_id,
            "match": {
                "channel": "telegram",
                "accountId": account_id,
                "peer": {"kind": "dm", "id": tg_id},
            },
        }
        filtered = []
        for item in bindings:
            match = item.get("match", {})
            peer = match.get("peer", {})
            if (
                match.get("channel") == "telegram"
                and peer.get("kind") == "dm"
                and str(peer.get("id")) == str(tg_id)
            ):
                continue
            filtered.append(item)
        filtered.append(target)
        config["bindings"] = filtered
        return self._write_config(
            config,
            note=f"bind telegram dm peer {tg_id} to agent {agent_id}",
            changed_paths=["bindings"],
            extra={"removed_existing_for_tg_id": len(bindings) - len(filtered) + 1},
        )

    def _remove_peer_binding(self, tg_id: str) -> Dict[str, object]:
        config = self._load_config()
        bindings = list(config.get("bindings", []))
        filtered = []
        removed = 0
        for item in bindings:
            match = item.get("match", {})
            peer = match.get("peer", {})
            if (
                match.get("channel") == "telegram"
                and peer.get("kind") == "dm"
                and str(peer.get("id")) == str(tg_id)
            ):
                removed += 1
                continue
            filtered.append(item)
        config["bindings"] = filtered
        result = self._write_config(
            config,
            note=f"remove telegram dm peer binding {tg_id}",
            changed_paths=["bindings"],
            extra={"removed": removed},
        )
        return result

    def _set_agent_model_in_config(
        self,
        config: Dict[str, object],
        agent_id: str,
        model: str,
    ) -> Dict[str, object]:
        if self.runner.dry_run:
            return {
                "skipped": True,
                "reason": "model update needs live config rewrite",
            }
        agents = list(config.get("agents", {}).get("list", []))
        index, agent = self._find_agent(agents, agent_id)
        agent["model"] = model
        config.setdefault("agents", {})["list"] = agents
        result = self._write_config(
            config,
            note=f"set model for agent {agent_id}",
            changed_paths=["agents.list"],
        )
        return {"index": index, "result": result}

    def _remove_tg_id_from_all_accounts(self, tg_id: str) -> Dict[str, object]:
        config = self._load_config()
        accounts = (
            config.setdefault("channels", {})
            .setdefault("telegram", {})
            .setdefault("accounts", {})
        )
        changed = []
        for account_id, account in accounts.items():
            allow_from = account.get("allowFrom")
            if not isinstance(allow_from, list):
                continue
            filtered = [str(item) for item in allow_from if str(item) != str(tg_id)]
            if filtered != [str(item) for item in allow_from]:
                account["allowFrom"] = filtered
                changed.append(account_id)
        return self._write_config(
            config,
            note=f"remove tg id {tg_id} from telegram allowlists",
            changed_paths=[f"channels.telegram.accounts.{item}.allowFrom" for item in changed],
            extra={"accounts_updated": changed},
        )

    def _purge_workspace(self, workspace: str) -> Dict[str, object]:
        target = Path(workspace).expanduser().resolve()
        if self.runner.dry_run:
            return {"skipped": True, "path": str(target)}
        if not target.exists():
            return {"deleted": False, "path": str(target), "reason": "not_found"}
        shutil.rmtree(target)
        return {"deleted": True, "path": str(target)}

    def _safe_unbind(self, agent_id: str, account_id: str) -> Dict[str, object]:
        try:
            result = self.runner.run_json(
                [
                    self.bin,
                    "agents",
                    "unbind",
                    "--agent",
                    agent_id,
                    "--bind",
                    f"telegram:{account_id}",
                    "--json",
                ]
            )
            return {"step": "rollback.agents.unbind", "result": result}
        except Exception as exc:
            return {"step": "rollback.agents.unbind", "error": str(exc)}

    def _safe_tg_remove(self, account_id: str) -> Dict[str, object]:
        try:
            result = self.tg_remove(account_id)
            return {"step": "rollback.tg.remove", "result": result}
        except Exception as exc:
            return {"step": "rollback.tg.remove", "error": str(exc)}

    def _safe_tg_remove_from_all_accounts(self, tg_id: str) -> Dict[str, object]:
        try:
            result = self._remove_tg_id_from_all_accounts(tg_id)
            return {"step": "rollback.tg.whitelist.remove", "result": result}
        except Exception as exc:
            return {"step": "rollback.tg.whitelist.remove", "error": str(exc)}

    def _safe_delete_agent(self, agent_id: str) -> Dict[str, object]:
        try:
            result = self.runner.run_json(
                [self.bin, "agents", "delete", agent_id, "--force", "--json"]
            )
            return {"step": "rollback.agents.delete", "result": result}
        except Exception as exc:
            return {"step": "rollback.agents.delete", "error": str(exc)}

    def _safe_remove_peer_binding(self, tg_id: str) -> Dict[str, object]:
        try:
            result = self._remove_peer_binding(tg_id)
            return {"step": "rollback.bindings.remove", "result": result}
        except Exception as exc:
            return {"step": "rollback.bindings.remove", "error": str(exc)}

    def default_workspace(self, agent_id: str) -> str:
        return f"/data/openclaw/{agent_id}"

    def resolve_agent_id(self, tg_id: str, agent_name: Optional[str]) -> str:
        return agent_name or tg_id

    def _error_details(self, exc: Exception) -> Dict[str, object]:
        if isinstance(exc, CommandError):
            return {
                "command": exc.result.command_text,
                "returncode": exc.result.returncode,
                "stdout": exc.result.stdout,
                "stderr": exc.result.stderr,
            }
        return {}

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
        payload["note"] = note
        return payload
