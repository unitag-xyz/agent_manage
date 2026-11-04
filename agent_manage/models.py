from __future__ import annotations

from dataclasses import dataclass

from .settings import DEFAULT_AI_SHOP, DEFAULT_IMAGE_QUALITY, DEFAULT_MODEL_ENV


@dataclass
class CreateInstanceRequest:
    template_name: str = ""
    model_key: str = ""
    model_env: str = DEFAULT_MODEL_ENV
    ai_shop: str = DEFAULT_AI_SHOP
    model: str | None = None
    image_quality: str = DEFAULT_IMAGE_QUALITY
    base_url: str | None = None
    workspace_root: str = "~/data"
    rollback_on_fail: bool = True
    agent_zip: str | None = None
    local: bool = False


@dataclass
class AddAgentRequest:
    agent_name: str
    template_name: str | None = None
    workspace: str | None = None
    model: str | None = None
    template_zip_url: str | None = None
    template_zip_sha256: str | None = None


@dataclass
class AddAgentsRequest:
    agents: list[AddAgentRequest]
    workspace_root: str = "~/data"
    base_url: str | None = None


@dataclass
class AddSkillRequest:
    agent_name: str | None = None
    common: bool = False
    skill_dir: str | None = None
    skill_zip: str | None = None
    skill_name: str | None = None
    replace: bool = False


@dataclass
class RefreshAgentRequest:
    agent_name: str
    template_dir: str | None = None
    agent_zip: str | None = None
    template_name: str | None = None
    models_only: bool = False
    template_only: bool = False
    replace_modified: bool = False
    restart: bool = False
    template_version: str | None = None
    template_zip_url: str | None = None
    template_zip_sha256: str | None = None


@dataclass
class AddTelegramBotRequest:
    agent_name: str
    bot_token: str
    bot_name: str | None = None


@dataclass
class AddFeishuBotRequest:
    agent_name: str
    app_id: str
    app_secret: str
    domain: str = "feishu"
    account_id: str = "main"
    bot_name: str | None = None
    dm_policy: str = "open"
    allow_from: list[str] | None = None
    bind_lark_cli: bool = False
    lark_cli_identity: str = "bot-only"


@dataclass
class AddWeixinBotRequest:
    agent_name: str
    ilink_bot_id: str
    bot_token: str
    baseurl: str | None = None
    ilink_user_id: str | None = None
    bot_name: str | None = None
    route_tag: str | None = None
    cdn_base_url: str | None = None


@dataclass
class DeleteTelegramBotRequest:
    bot_name: str


@dataclass
class DeleteFeishuBotRequest:
    account_id: str


@dataclass
class DeleteWeixinBotRequest:
    ilink_bot_id: str


@dataclass
class SetModelRequest:
    model_ref: str
