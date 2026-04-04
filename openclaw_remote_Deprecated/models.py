from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class TelegramAccountConfig:
    account_id: str
    bot_token: Optional[str] = None


@dataclass
class CreateAgentRequest:
    tg_id: str
    agent_name: Optional[str] = None
    model: Optional[str] = None
    telegram: Optional[TelegramAccountConfig] = None
    agent_dir: Optional[str] = None
    rollback_on_fail: bool = True


@dataclass
class DeleteAgentRequest:
    tg_id: str
    agent_name: Optional[str] = None
    force: bool = True
    purge_workspace: bool = False
