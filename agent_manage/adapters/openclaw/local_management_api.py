"""Map unified management requests to OpenClaw local execution operations."""

import re
from pathlib import Path

from ... import models
from ...core.errors import APIError


def register_secrets(manager, value):
    if isinstance(value, dict):
        for key, child in value.items():
            if re.search(r"key|token|secret|password", key, re.I) and isinstance(child, str):
                manager.register_secret(child)
            else:
                register_secrets(manager, child)
    elif isinstance(value, list):
        for child in value:
            register_secrets(manager, child)


def invoke_local_management(manager, method, params, *, context, agent_name):
    options = params.get("options", {})
    # CamelCase belongs to v1; snake_case stays private to retained dataclasses.
    options = {re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower(): value for key, value in options.items()}
    for field in ("workspace_root", "template_dir", "agent_zip", "skill_dir", "skill_zip"):
        value = options.get(field)
        if isinstance(value, str):
            if value == "~" or value.startswith("~/"):
                value = str(context.home) + value[1:]
                options[field] = value
            if not Path(value).is_absolute():
                raise APIError("INVALID_ARGUMENT", f"{field} must be an absolute path in the selected environment")
    register_secrets(manager, options)
    if method == "agents.create":
        options.setdefault("workspace_root", str(context.state_dir / "data"))
        return manager.operation("add_agent")(**options)
    if method == "dola.runtime.configure":
        return manager.operation("configure_instance")(models.CreateInstanceRequest(**options))
    if method in ("agents.refresh", "dola.agents.refresh", "agents.install"):
        name = agent_name(params["agentId"])
        if method == "agents.refresh":
            if options.get("models_only") or options.get("template_only") is False:
                raise APIError("INVALID_ARGUMENT", "Model catalog refresh belongs to dola.agents.refresh")
            options["template_only"] = True
        if method == "agents.install":
            return manager.operation("install_agent_service")(name)
        return manager.operation("refresh_agent")(models.RefreshAgentRequest(agent_name=name, **options))
    if method == "skills.install":
        return manager.operation("add_skill")(models.AddSkillRequest(**options))
    simple = {"models.list": "get_supported_models", "models.get": "get_current_model", "dola.models.refresh": "update_model_catalog",
              "connections.get": "codex_status", "connections.login": "codex_login", "connections.logout": "codex_logout",
              "services.start": "start_agent_services"}
    if method in simple:
        return manager.operation(simple[method])()
    if method == "models.select":
        return manager.operation("set_model")(models.SetModelRequest(params["modelId"]))
    channels = {"telegram": ("tg", models.AddTelegramBotRequest, models.DeleteTelegramBotRequest),
                "feishu": ("feishu", models.AddFeishuBotRequest, models.DeleteFeishuBotRequest),
                "weixin": ("weixin", models.AddWeixinBotRequest, models.DeleteWeixinBotRequest)}
    if method == "channels.list":
        return {"items": [{"channel": name, "status": manager.operation(f"get_{entry[0]}_bot_status")()}
                           for name, entry in channels.items() if not params.get("channel") or name == params["channel"]]}
    if method in ("channels.configure", "channels.remove"):
        if params["channel"] not in channels:
            raise APIError("INVALID_ARGUMENT", "Unknown channel")
        name, add, remove = channels[params["channel"]]
        return manager.operation(f"add_{name}_bot" if method.endswith("configure") else f"delete_{name}_bot")(
            (add if method.endswith("configure") else remove)(**options))
    raise APIError("UNSUPPORTED", "Method is not implemented by this adapter")
