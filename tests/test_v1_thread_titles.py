import unittest
from unittest.mock import AsyncMock

from agent_manage.adapters.openclaw.v2026_7.thread_titles import enrich_titles, project_titles
from agent_manage.core.errors import APIError


class ThreadTitleTest(unittest.IsolatedAsyncioTestCase):
    async def test_native_pages_enrich_existing_rows_only(self):
        targets = {f"agent:main:{i}": (f"session-{i}", {"title": None}, False) for i in range(101)}
        gateway = AsyncMock()

        def page(method, params):
            self.assertEqual("sessions.list", method)
            self.assertEqual("main", params["agentId"])
            self.assertTrue(params["includeDerivedTitles"] and params["includeLastMessage"])
            start = params.get("offset", 0)
            self.assertEqual(1 if start else 100, params["limit"])
            rows = [{"key": f"agent:main:{i}", "sessionId": f"session-{i}", "derivedTitle": f"native {i}"}
                    for i in range(start, start + params["limit"])]
            if not start:
                rows[0]["sessionId"] = "reset-session"
                rows[1]["key"] = "agent:foreign:1"
            return {"sessions": rows, "hasMore": start == 0, "nextOffset": 100 if start == 0 else None}

        gateway.call.side_effect = page
        await enrich_titles(gateway, targets)
        self.assertEqual(2, gateway.call.await_count)
        self.assertEqual({"title": None}, targets["agent:main:0"][1])
        self.assertEqual({"title": None}, targets["agent:main:1"][1])
        self.assertEqual({"title": "native 100", "derivedTitle": "native 100"}, targets["agent:main:100"][1])

    async def test_archived_and_local_only_rows(self):
        gateway = AsyncMock()
        gateway.call.return_value = {"sessions": [{"key": "agent:a:one", "sessionId": "s",
            "label": "renamed", "derivedTitle": "renamed", "lastMessagePreview": "not the title"}]}
        row = {"title": None}
        await enrich_titles(gateway, {"agent:a:one": ("s", row, True), "agent:b:key": (None, {}, False)})
        self.assertEqual({"title": "renamed", "derivedTitle": "renamed"}, row)
        gateway.call.assert_awaited_once_with("sessions.list", {"agentId": "a", "limit": 1, "archived": True,
            "includeDerivedTitles": True, "includeLastMessage": True})

    async def test_gateway_unavailable_preserves_index_title(self):
        gateway = AsyncMock()
        gateway.call.side_effect = APIError("NOT_READY", "offline")
        row = project_titles({"label": "renamed", "displayName": "previous"})
        await enrich_titles(gateway, {"agent:main:key": ("s", row, False)}, detail=True)
        self.assertEqual({"title": "renamed"}, row)
        gateway.call.side_effect = None
        gateway.call.return_value = {"session": {"key": "agent:main:key", "sessionId": "s"}}
        await enrich_titles(gateway, {"agent:main:key": ("s", row, False)}, detail=True)
        self.assertEqual({"title": "renamed"}, row)
