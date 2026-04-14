from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CreateInstanceRequest:
    template_name: str
    model_key: str
    model: str | None = None
    workspace_root: str = "~/data"
    rollback_on_fail: bool = True


@dataclass
class AddTelegramBotRequest:
    agent_name: str
    bot_token: str
    bot_name: str | None = None


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
class DeleteWeixinBotRequest:
    ilink_bot_id: str


@dataclass
class SetModelRequest:
    model_ref: str
