"""Private Host Gateway exchange for platform-scoped Chat, not a browser endpoint."""

import asyncio
import json
import re
import time
from dataclasses import dataclass

from .core.errors import APIError
from .core.contracts import INSTANCE_CHAT_ADDITIONS


_INSTANCE = re.compile(r"[a-z0-9][a-z0-9-]{1,46}[a-z0-9]")
_GENERATION = re.compile(r"[0-9a-f]{64}")
_ID = re.compile(r"[A-Za-z0-9_-]{1,120}")
_CHAT_SESSION = "chat.session"
_CHAT_INSTANCE = "chat.instance"
# Browser Chat grants are expanded only here, on the private platform exchange.
_ACTIONS = frozenset({"agents.list", "threads.list", "threads.create", "threads.get", "threads.update",
    "segments.list", "messages.list", "messages.get", "runs.create", "runs.get", "runs.lookup",
    "runs.cancel", "operations.get", "events.subscribe", "events.unsubscribe"})


@dataclass(frozen=True)
class PlatformChatGrant:
    issuer: str
    subject: str
    tenant_id: str
    runtime_id: str
    binding_id: str
    agent_identify: str
    native_agent_id: str
    installation_id: str
    actions: tuple[str, ...]
    expires_at: int
    instance_chat: bool = False
    template_identify: str | None = None
    attached_installations: tuple[tuple[str, str, str], ...] = ()


def _identifier(value, pattern=_ID):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _attached_installations(extensions):
    rows = extensions.get("attachedInstallations", [])
    if not isinstance(rows, list) or len(rows) > 64:
        return ()
    fields = ("nativeAgentId", "platformAgentIdentify", "installationId")
    candidates = [tuple(row[field] for field in fields) for row in rows
                  if isinstance(row, dict) and all(_identifier(row.get(field)) for field in fields)]
    return tuple(row for row in candidates
                 if all(sum(other[index].lower() == row[index].lower() for other in candidates) == 1 for index in range(3)))


def _grant_extensions(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise APIError("FORBIDDEN", "Invalid platform grant extensions")
    try:
        if len(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()) > 4096:
            raise ValueError()
    except (TypeError, ValueError, RecursionError) as exc:
        raise APIError("FORBIDDEN", "Invalid platform grant extensions") from exc
    pending = [(value, 1)]
    while pending:
        item, depth = pending.pop()
        if depth > 8:
            raise APIError("FORBIDDEN", "Invalid platform grant extensions")
        pending.extend((child, depth + 1) for child in (item.values() if isinstance(item, dict) else item)
                       if isinstance(child, (dict, list)))
    return value


async def request_platform_grant(ws, runtime_id, binding_id, auth_frame, *, clock=time.time):
    """Consume a browser first frame via a callback on the already private socket."""
    if not _identifier(runtime_id, _INSTANCE) or not _identifier(binding_id, _GENERATION):
        raise APIError("FORBIDDEN", "Invalid Host binding")
    if (not isinstance(auth_frame, dict) or set(auth_frame) != {"type", "ticket"}
            or auth_frame.get("type") != "auth"
            or not _identifier(auth_frame.get("ticket"), _GENERATION)):
        raise APIError("UNAUTHENTICATED", "A platform ticket is required")

    # Gateway holds the original first frame. The Manager cannot redirect the callback or replace its ticket.
    await asyncio.wait_for(ws.send_json({"type": "platform.redeem", "runtimeId": runtime_id,
        "bindingId": binding_id}), 5)
    reply = await asyncio.wait_for(ws.receive_json(), 20)
    valid_keys = {"type", "runtimeId", "bindingId", "issuer", "subject", "tenantId", "agentIdentify",
        "nativeAgentId", "installationId", "actions", "expiresAt"}
    if (not isinstance(reply, dict) or not valid_keys <= reply.keys()
            or reply.keys() - valid_keys - {"templateIdentify", "extensions"}
            or reply["type"] != "platform.grant"
            or reply["runtimeId"] != runtime_id or reply["bindingId"] != binding_id
            or any(not _identifier(reply[key]) for key in ("agentIdentify", "nativeAgentId", "installationId"))
            or any(not isinstance(reply[key], str) or not 1 <= len(reply[key]) <= 128
                   for key in ("issuer", "subject", "tenantId"))):
        raise APIError("FORBIDDEN", "Platform grant does not match the Host binding")
    actions, expires_at = reply["actions"], reply["expiresAt"]
    now = int(clock() * 1000)
    if type(expires_at) is not int or not now < expires_at <= now + 300_000:
        raise APIError("FORBIDDEN", "Platform grant scope or expiry is invalid")
    instance_chat = actions == [_CHAT_INSTANCE]
    if instance_chat:
        actions = sorted(_ACTIONS | INSTANCE_CHAT_ADDITIONS)
    elif actions == [_CHAT_SESSION]:
        actions = sorted(_ACTIONS)
    elif (not isinstance(actions, list) or not 1 <= len(actions) <= len(_ACTIONS)
            or any(not isinstance(action, str) or action not in _ACTIONS for action in actions)
            or len(set(actions)) != len(actions)):
        raise APIError("FORBIDDEN", "Platform grant scope or expiry is invalid")
    extensions = _grant_extensions(reply.get("extensions"))
    if reply.get("extensions") is None:
        template = reply.get("templateIdentify")
    else:
        presentation = extensions.get("presentation")
        template = presentation.get("templateIdentify") if isinstance(presentation, dict) else None
    return PlatformChatGrant(reply["issuer"], reply["subject"], reply["tenantId"], runtime_id, binding_id,
        reply["agentIdentify"], reply["nativeAgentId"], reply["installationId"], tuple(actions), expires_at, instance_chat,
        template if instance_chat and _identifier(template) else None,
        _attached_installations(extensions) if instance_chat else ())
