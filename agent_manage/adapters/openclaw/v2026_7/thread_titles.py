"""Project OpenClaw's session titles; only the Gateway derives them from transcripts."""
import asyncio
import re

from ....core.errors import APIError

_SESSION_DATE_SUFFIX = re.compile(r" \([0-9]{4}-[0-9]{2}-[0-9]{2}\)")


def project_titles(entry):
    def text(field):
        value = entry.get(field)
        return value.strip()[:256] if isinstance(value, str) and value.strip() else None

    session_id = entry.get("sessionId")

    def automatic(field):
        value = text(field)
        if value and isinstance(session_id, str) and session_id:
            prefix = session_id[:8]
            if value in (session_id, prefix) or (
                value.startswith(prefix) and _SESSION_DATE_SUFFIX.fullmatch(value[len(prefix):])
            ):
                return None
        return value

    derived = automatic("derivedTitle")
    return {"title": text("label") or automatic("displayName") or derived,
            **({"derivedTitle": derived} if derived else {})}


async def enrich_titles(gateway, targets, *, detail=False):
    if not gateway or not targets:
        return
    flags = {"includeDerivedTitles": True}

    def apply(native, allowed):
        if not isinstance(native, dict) or not isinstance(native.get("key"), str):
            return
        target = allowed.get(native["key"])
        if not target or not isinstance(target[0], str) or not target[0] or target[0] != native.get("sessionId"):
            return  # Different physical session (e.g. reset between the two reads).
        row = target[1]
        titles = project_titles(native)
        if titles["title"] is None:
            return
        row.pop("derivedTitle", None)
        row.update(titles)

    async def read():
        if detail:
            key = next(iter(targets))
            if not targets[key][0]:
                return
            data = await gateway.call("sessions.describe", {"key": key, **flags})
            if isinstance(data, dict):
                apply(data.get("session"), targets)
            return

        groups = {}
        for key, target in targets.items():
            _, agent, _ = key.split(":", 2)
            groups.setdefault((agent, target[2]), {})[key] = target
        for (agent, archived), allowed in groups.items():
            if not any(target[0] for target in allowed.values()):
                continue
            offset = 0
            while offset < len(allowed):
                # July computes transcript fields for at most 100 rows per native page.
                size = min(100, len(allowed) - offset)
                data = await gateway.call("sessions.list", {"agentId": agent, "limit": size,
                    **({"offset": offset} if offset else {}), **({"archived": True} if archived else {}), **flags})
                sessions = data.get("sessions") if isinstance(data, dict) else None
                if not isinstance(sessions, list) or len(sessions) > size:
                    break
                for native in sessions:
                    apply(native, allowed)
                offset += len(sessions)
                if not sessions or data.get("hasMore") is not True or data.get("nextOffset") != offset:
                    break

    try:
        await asyncio.wait_for(read(), 5)
    except (APIError, asyncio.TimeoutError):
        # Optional display metadata must not make the existing local index unavailable.
        pass
