from __future__ import annotations

import json
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from .local import CommandError, LocalRunner

from .models import (
    AddTelegramBotRequest,
    CreateInstanceRequest,
    DeleteTelegramBotRequest,
    SUPPORTED_MODEL_REFS,
    SetModelRequest,
)


class InstanceManagerV2:
    SERVER_STATUS_TIMEOUT_SECONDS = 10

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
        steps: List[Dict[str, object]] = []
        agent_name = self.resolve_agent_name(request)
        workspace = self.default_workspace(agent_name, request.workspace_root)
        archive_path = self.resolve_archive_path(request)
        template_dir = self.resolve_template_dir(request)

        self._ensure_sources_ready(archive_path=archive_path)
        self._ensure_workspace_available(workspace)
        self._ensure_agent_available(agent_name)

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

            agent_result = self._add_agent(
                agent_name=agent_name,
                workspace=workspace,
                model=request.model,
            )
            created_agent = True
            steps.append({"step": "agents.add", "result": agent_result})

            workspace_result = self._populate_workspace(
                template_dir=template_dir,
                workspace=workspace,
            )
            created_workspace = not workspace_result.get("skipped", False)
            steps.append({"step": "workspace.populate", "result": workspace_result})

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
        self._ensure_agent_exists(request.agent_name)

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
        return {
            "ok": True,
            "agent_name": request.agent_name,
            "bot_name": account_id,
            "config_write": write_result,
        }

    def check_server_status(self) -> Dict[str, object]:
        gateway_status = self.runner.run_json(
            [self.bin, "gateway", "status", "--require-rpc", "--json"],
            timeout=self.SERVER_STATUS_TIMEOUT_SECONDS,
        )
        tg_bot_status = self.get_tg_bot_status()

        return {
            "ok": True,
            "check": "openclaw gateway status --require-rpc --json",
            "timeout_seconds": self.SERVER_STATUS_TIMEOUT_SECONDS,
            "config_path": str(self.config_path),
            "config_exists": self.config_path.exists(),
            "gateway_status": self._summarize_gateway_status(gateway_status),
            "tg_bot_status": tg_bot_status,
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

    def set_model(self, request: SetModelRequest) -> Dict[str, object]:
        model_ref = SUPPORTED_MODEL_REFS.get(request.model_name)
        if not model_ref:
            allowed = ", ".join(sorted(SUPPORTED_MODEL_REFS.keys()))
            raise ValueError(f"Unsupported model '{request.model_name}'. Allowed: {allowed}")

        set_result = self.runner.run([self.bin, "models", "set", model_ref])

        return {
            "ok": True,
            "model_name": request.model_name,
            "model_ref": model_ref,
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

    def _ensure_workspace_available(self, workspace: Path) -> None:
        if not workspace.exists():
            return
        if not workspace.is_dir():
            raise NotADirectoryError(f"Workspace path is not a directory: {workspace}")
        if any(workspace.iterdir()):
            raise FileExistsError(f"Workspace already exists and is not empty: {workspace}")

    def _ensure_agent_available(self, agent_name: str) -> None:
        if self.runner.dry_run:
            return
        agents = self.runner.run_json([self.bin, "agents", "list", "--bindings", "--json"])
        for item in self._extract_agent_list(agents):
            if item.get("id") == agent_name:
                raise FileExistsError(f"Agent already exists: {agent_name}")

    def _ensure_agent_exists(self, agent_name: str) -> None:
        if self.runner.dry_run:
            return
        agents = self.runner.run_json([self.bin, "agents", "list", "--bindings", "--json"])
        for item in self._extract_agent_list(agents):
            if item.get("id") == agent_name:
                return
        raise FileNotFoundError(f"Agent not found: {agent_name}")

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
