from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CreateInstanceRequest:
    template_name: str
    model: str | None = None
    workspace_root: str = "~/data"
    rollback_on_fail: bool = True


@dataclass
class AddTelegramBotRequest:
    agent_name: str
    bot_token: str
    bot_name: str | None = None


@dataclass
class DeleteTelegramBotRequest:
    bot_name: str
