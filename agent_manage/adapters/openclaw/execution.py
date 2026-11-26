"""Offline OpenClaw operations shared by the CLI and the versioned runtime adapter."""

import json
import os

from ...core.errors import APIError
from .manager_core import ManagerCore
from .local import LocalRunner
from .orchestrator import InstanceManagerV2
from ..execution import LocalOptions


class OpenClawExecution:
    operations = frozenset({
        "create_instance", "configure_instance", "activate_instance", "install_agent_service",
        "start_agent_services", "add_agent", "add_agents", "refresh_agent", "add_skill",
        "codex_login", "codex_logout", "codex_status", "list_agents", "check_server_status",
        "get_current_gateway_token", "get_supported_models", "get_current_model",
        "update_model_catalog", "set_model", "add_tg_bot", "delete_tg_bot", "get_tg_bot_status",
        "add_feishu_bot", "delete_feishu_bot", "get_feishu_bot_status", "add_weixin_bot",
        "delete_weixin_bot", "get_weixin_bot_status", "segment_history", "segment_message_get",
        "segment_media_get",
    })

    def __init__(self, manager, *, container, cli_command=None):
        self._manager = manager
        self.container = container
        self.cli_command = cli_command

    def operation(self, name):
        if name not in self.operations:
            raise APIError("UNSUPPORTED", "Operation is not supported by the OpenClaw adapter")
        if self.cli_command == "agents-start" and name == "start_agent_services":
            return self._start_from_cli
        return getattr(self._manager, name)

    def _start_from_cli(self):
        result = self._manager.start_agent_services()
        if not result["ok"]:
            raise RuntimeError(json.dumps({
                "error": "One or more agent start.sh scripts failed",
                "details": {"returncode": 1, "agents": result["agents"]},
            }))
        return result

    @property
    def restart_required(self):
        return self._manager.restart_required

    def register_secret(self, value):
        self._manager.runner.add_redaction_value(value)

    def default_workspace(self, *, local=False):
        if self.container:
            return ManagerCore.CONTAINER_WORKSPACE_ROOT
        return ManagerCore.LOCAL_WORKSPACE_ROOT if local else "~/data"


def open_local(options: LocalOptions):
    if options.command == "activate-instance":
        InstanceManagerV2.require_fly_activation()
    executable = "openclaw" if options.executable is None else options.executable
    manager = InstanceManagerV2(LocalRunner(executable, options.project_dir, options.dry_run),
                                template_root=options.template_root, config_path=options.config_path)
    return OpenClawExecution(manager, container=os.environ.get("UNITAG_AGENT_MANAGER_RUNTIME") == "container",
                             cli_command=options.command)


def open_registered(context, template_root, registration):
    manager = InstanceManagerV2(LocalRunner(context.executable), template_root=template_root,
                                config_path=str(context.config_path))
    manager.runtime_context = context
    if "environmentKind" in registration:
        manager.container_runtime = registration["environmentKind"] == "container"
    return OpenClawExecution(manager, container=manager.container_runtime)
