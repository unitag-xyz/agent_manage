import unittest
from unittest.mock import AsyncMock

from agent_manage.adapters.openclaw.v2026_7.thread_titles import enrich_titles, project_titles
from agent_manage.core.errors import APIError


class ThreadTitleTest(unittest.IsolatedAsyncioTestCase):
    def test_title_fallback_is_scoped_to_the_native_session(self):
        entry = {"sessionId": "adfea059-1234-4234-8234-0123456789ab",
                 "lastMessagePreview": "Not a title"}
        for fallback in (entry["sessionId"], "adfea059", "adfea059 (2026-11-15)"):
            with self.subTest(fallback=fallback):
                row = dict(entry, displayName=fallback, derivedTitle=fallback)
                self.assertEqual({"title": None}, project_titles(row))
                self.assertEqual({"title": fallback}, project_titles(dict(row, label=fallback)))
        self.assertEqual({"title": None}, project_titles(entry))
        for title in ("hihi", "different-id", "deadbeef (2026-11-15)", "adfea059 notes"):
            self.assertEqual({"title": title, "derivedTitle": title},
                             project_titles(dict(entry, derivedTitle=title)))

    async def test_native_list_and_detail_do_not_fill_title_with_id_or_preview(self):
        key = "agent:main:old"
        session_id = "adfea059-1234-4234-8234-0123456789ab"
        native = {"key": key, "sessionId": session_id, "derivedTitle": "adfea059 (2026-11-15)",
                  "lastMessagePreview": "A recent reply"}
        gateway = AsyncMock()
        for detail in (False, True):
            with self.subTest(detail=detail):
                gateway.call.return_value = {"session": native} if detail else {"sessions": [native]}
                for indexed in ({"title": None}, {"title": "Indexed", "derivedTitle": "Indexed"}):
                    row = indexed.copy()
                    await enrich_titles(gateway, {key: (session_id, row, False)}, detail=detail)
                    self.assertEqual(indexed, row)

    async def test_native_pages_enrich_existing_rows_only(self):
        targets = {f"agent:main:{i}": (f"session-{i}", {"title": None}, False) for i in range(101)}
        gateway = AsyncMock()

        def page(method, params):
            self.assertEqual("sessions.list", method)
            self.assertEqual("main", params["agentId"])
            self.assertTrue(params["includeDerivedTitles"])
            self.assertNotIn("includeLastMessage", params)
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
            "includeDerivedTitles": True})

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
