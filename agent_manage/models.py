from __future__ import annotations

from dataclasses import dataclass

SUPPORTED_MODEL_REFS = {
    "gpt-5.4": "unipay-fun/gpt-5.4",
    "gpt-5.4-mini": "unipay-fun/gpt-5.4-mini",
    "gpt-4.1-mini": "unipay-fun/gpt-4.1-mini",
    "gpt-5.3-codex": "unipay-fun/gpt-5.3-codex",
}


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


@dataclass
class SetModelRequest:
    model_name: str
