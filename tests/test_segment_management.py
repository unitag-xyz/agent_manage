import base64
import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from agent_manage.cli import main
from agent_manage.adapters.openclaw.local import LocalRunner
from agent_manage.adapters.openclaw.orchestrator import InstanceManagerV2
from agent_manage.response import ProtocolError, build_error_response, build_success_response, print_json
from agent_manage.adapters.openclaw.v2026_7.segment_reader import SegmentReader, read_segment, select_rows


class SegmentCliTest(unittest.TestCase):
    def test_timed_output_only_encodes_large_payload_once(self):
        from agent_manage.segment_timing import CURRENT, ReadTiming
        marker = "界" * 20000
        token = CURRENT.set(ReadTiming())
        self.addCleanup(CURRENT.reset, token)
        output = io.StringIO()
        encodings = []
        dumps = json.dumps
        def measure(value, **kwargs):
            encoded = dumps(value, **kwargs)
            if marker in encoded:
                encodings.append(len(encoded))
            return encoded
        with patch("agent_manage.response.json.dumps", side_effect=measure), redirect_stdout(output), redirect_stderr(io.StringIO()):
            print_json(build_success_response({"payload": marker, "timings": {}}))
        result = json.loads(output.getvalue())["result"]
        self.assertEqual(marker, result["payload"])
        self.assertIn("responseSerializationMs", result["timings"]["phasesMs"])
        self.assertEqual(1, len(encodings))

    def test_timed_budget_checks_final_utf8_bytes_for_success_and_error(self):
        from agent_manage.segment_timing import CURRENT, ReadTiming
        for body in (build_success_response({"text": '界 "timings": null', "nested": {"timings": None}}),
                     build_error_response(ValueError('失败 "timings": null'))):
            def emit(limit):
                timer = ReadTiming()
                stdout, stderr = io.StringIO(), io.StringIO()
                key = CURRENT.set(timer)
                try:
                    with (patch.object(timer, "snapshot", return_value={"fixture": "真实"}),
                          patch("agent_manage.response.MAX_PROTOCOL_RESPONSE_BYTES", limit),
                          redirect_stdout(stdout), redirect_stderr(stderr)):
                        print_json(body)
                finally:
                    CURRENT.reset(key)
                encoded = stdout.getvalue()
                self.assertIn(f"emitted_utf8_bytes={len(encoded.encode())} ", stderr.getvalue())
                return json.loads(encoded), len(encoded.encode())
            expected, size = emit(10000)
            self.assertEqual(emit(size)[0], expected)
            refused, _ = emit(size - 1)
            self.assertEqual(refused["error"], {"code": "RESPONSE_OUTPUT_TOO_LARGE", "details": {"totalUtf8Bytes": size}})

    def test_session_key_alias_is_explicit_for_all_segment_commands(self):
        cases = [
            ("sessions-segments-history", "segment_history", ["--edge", "end", "--limit", "1"],
             {"edge": "end", "limit": 1, "cursor": None, "view": None}, ()),
            ("sessions-segments-message-get", "segment_message_get", ["--message-id", "entry", "--ref", "snapshot"],
             {"ref": "snapshot", "cursor": None}, ("entry",)),
            ("sessions-segments-media-get", "segment_media_get", ["--ref", "media"],
             {"cursor": None}, ("media",)),
        ]
        # Disabling automatic abbreviation proves the alias is declared explicitly.
        from agent_manage.response import JsonArgumentParser
        original_init = JsonArgumentParser.__init__

        def no_abbreviation(parser, *args, **kwargs):
            kwargs["allow_abbrev"] = False
            original_init(parser, *args, **kwargs)

        with patch.object(JsonArgumentParser, "__init__", no_abbreviation):
            for option in ("--session-key", "--session-k"):
                for command, method, extra, expected, positional in cases:
                    with self.subTest(command=command, option=option):
                        with patch.object(InstanceManagerV2, method, return_value={}) as call:
                            with redirect_stdout(io.StringIO()):
                                self.assertEqual(main([command, option, "agent:main:thread:x", "--session-id", "old", *extra]), 0)
                            call.assert_called_once_with("agent:main:thread:x", "old", *positional, **expected)

    def test_omit_thinking_cli_forwards_flag(self):
        for flags, expected in ((["--omit-thinking"], True), (["--omit-thinking", "true"], True),
                                (["--omit-thinking=true"], True), (["--omit-thinking", "TRUE"], True),
                                (["--omit-thinking", "false"], False), (["--omit-thinking=false"], False)):
            with self.subTest(flags=flags), patch.object(InstanceManagerV2, "segment_history", return_value={}) as call:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["sessions-segments-history", "--session-k", "agent:main:main",
                                           "--session-id", "old", *flags, "--limit", "50"]), 0)
                call.assert_called_once_with("agent:main:main", "old", edge=None, limit=50,
                                             cursor=None, view=None, omit_thinking=expected)

    def test_omit_thinking_rejects_invalid_boolean(self):
        with patch.object(InstanceManagerV2, "segment_history") as call:
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["sessions-segments-history", "--session-k", "agent:main:main",
                                       "--session-id", "old", "--omit-thinking", "maybe"]), 1)
            self.assertEqual(json.loads(output.getvalue())["error"]["code"], "INVALID_ARGUMENT")
            call.assert_not_called()

    def test_arguments_and_response(self):
        with patch.object(InstanceManagerV2, "segment_history", return_value={"messages": []}) as call:
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["sessions-segments-history", "--session-key", "agent:main:thread:x",
                                       "--session-id", "old", "--edge", "start", "--limit", "4"]), 0)
            call.assert_called_once_with("agent:main:thread:x", "old", edge="start", limit=4, cursor=None, view=None)
            result = json.loads(output.getvalue())["result"]
            self.assertEqual(result["messages"], [])
            self.assertIn("responseSerializationMs", result["timings"]["phasesMs"])

    def test_domain_error_code_survives_cli(self):
        with patch.object(InstanceManagerV2, "segment_history", side_effect=ProtocolError("FORBIDDEN", "Denied")):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["sessions-segments-history", "--session-key", "agent:main:thread:x", "--session-id", "old"]), 1)
            self.assertEqual(json.loads(output.getvalue())["error"]["code"], "FORBIDDEN")
            self.assertIn("timings", json.loads(output.getvalue()))

    def test_history_cursor_and_message_reference_forwarding(self):
        shared = ["--session-key", "agent:main:thread:x", "--session-id", "old"]
        with patch.object(InstanceManagerV2, "segment_history", return_value={}) as call:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["sessions-segments-history", *shared, "--cursor", "older", "--limit", "4"]), 0)
            call.assert_called_once_with("agent:main:thread:x", "old", edge=None, limit=4, cursor="older", view=None)
        with patch.object(InstanceManagerV2, "segment_message_get", return_value={}) as call:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["sessions-segments-message-get", *shared, "--message-id", "entry", "--ref", "snapshot"]), 0)
            call.assert_called_once_with("agent:main:thread:x", "old", "entry", ref="snapshot", cursor=None)

    def test_media_cursor_without_original_reference(self):
        with patch.object(InstanceManagerV2, "segment_media_get", return_value={"nextCursor": None}) as call:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["sessions-segments-media-get", "--session-key", "agent:main:thread:x",
                                       "--session-id", "old", "--cursor", "chunk"]), 0)
            call.assert_called_once_with("agent:main:thread:x", "old", None, cursor="chunk")

    def test_media_requires_reference_or_cursor(self):
        with patch.object(InstanceManagerV2, "_segment_bridge") as call:
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["sessions-segments-media-get", "--session-key", "agent:main:thread:x",
                                       "--session-id", "old"]), 1)
            call.assert_not_called()
            self.assertIn("ref or cursor", json.loads(output.getvalue())["message"])

    def test_compact_view_forwarding(self):
        with patch.object(InstanceManagerV2, "segment_history", return_value={}) as call:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["sessions-segments-history", "--session-k", "agent:main:main",
                                       "--session-id", "old", "--view", "compact"]), 0)
            call.assert_called_once_with("agent:main:main", "old", edge=None, limit=50, cursor=None, view="compact")

    def test_history_accepts_two_hundred_messages(self):
        manager = InstanceManagerV2(LocalRunner())
        with patch.object(manager, "_segment_bridge", return_value={}) as bridge:
            manager.segment_history("agent:main:main", "old", limit=200)
        self.assertEqual(bridge.call_args.kwargs["limit"], 200)

    def test_validation_before_native_process(self):
        manager = InstanceManagerV2(LocalRunner())
        for options in ({"limit": 0}, {"limit": 201}, {"limit": True}, {"edge": "start", "cursor": "abc"}):
            with self.assertRaises(ValueError):
                manager.segment_history("agent:main:main", "old", **options)
        for session_id in ("../secret", "", "a/b"):
            with self.assertRaises(ValueError):
                manager.segment_history("agent:main:main", session_id)


class DirectSegmentReaderTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="segment-direct-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.sessions = self.root / "agents/main/sessions"
        self.sessions.mkdir(parents=True)
        self.key = "agent:main:dashboard:case-Sensitive"
        self.request = {"action": "history", "root": str(self.root), "config": {},
                        "sessionKey": self.key, "sessionId": "physical", "edge": "end", "limit": 30}
        self.index = self.sessions / "sessions.json"
        self.index.write_text(json.dumps({self.key: {"sessionId": "physical", "usageFamilySessionIds": []}}))
        self.file = self.sessions / "physical.jsonl"
        self.file.write_text(json.dumps({"type": "message", "id": "tool", "parentId": None,
            "message": {"role": "toolResult", "toolName": "read", "isError": False,
                        "content": [{"type": "text", "text": "source code " * 10000}]}}) + "\n")

    def test_cli_chunk_location_is_optional_scoped_and_snapshot_checked(self):
        request = {**self.request, "action": "message", "messageId": "big"}
        env = {"AGENT_MANAGER_HISTORY_CURSOR_KEY": "private-fixture-secret"}
        def line(i, text):
            return json.dumps({"type":"message", "id":i, "message":{"role":"assistant","content":text}}) + "\n"
        original = line("small", "before") + line("big", "hello" * 100000)
        self.file.write_text(original)
        part = read_segment(request, env)
        reader = SegmentReader(request, env)
        signed = reader.decode(part["nextCursor"], "chunk")
        self.assertIn("record", signed)
        for auth in ({}, {"token": {"source": "env", "id": "TOKEN"}}, {"token": "${OPENCLAW_GATEWAY_TOKEN}"}):
            plain = {**request, "config": {"gateway": {"auth": auth}}}
            piece = read_segment(plain, {})
            self.assertNotIn("record", SegmentReader(plain, {}).decode(piece["nextCursor"], "chunk"))
        legacy = {k:v for k,v in signed.items() if k not in ("record", "messageHash")}
        # The optional hint must not push an otherwise valid CLI argument over its limit.
        padding = (8192 - len(reader.token(legacy))) * 3 // 4 - 12
        long_id = "big" + "a" * padding
        self.file.write_text(original.replace('"id": "big"', '"id": "' + long_id + '"'))
        long_part = read_segment({**request, "messageId": long_id}, env)
        self.assertLessEqual(len(long_part["nextCursor"]), 8192)
        long_claims = reader.decode(long_part["nextCursor"], "chunk")
        self.assertNotIn("record", long_claims)
        self.assertEqual(long_part["sha256"], long_claims["messageHash"])
        long_resume = {**request, "messageId": long_id, "cursor": long_part["nextCursor"]}
        self.assertEqual(long_part["sha256"], read_segment(long_resume, env)["sha256"])
        with self.assertRaises(ProtocolError) as raised:
            read_segment({**long_resume, "messageId": "small"}, env)
        self.assertEqual("CURSOR_EXPIRED", raised.exception.code)
        self.file.write_text(original)
        expected = read_segment({**request, "cursor":reader.token(legacy)}, env)
        resume = {**request, "cursor": part["nextCursor"]}
        with patch.object(SegmentReader, "scan", side_effect=AssertionError("unnecessary full scan")):
            with self.file.open("a") as f:
                f.write(line("appended", "later"))
            actual = read_segment(resume, env)
            for field in ("offset", "sha256", "data", "totalBytes", "nextCursor"):
                self.assertEqual(expected[field], actual[field])
            for invalid in ({**resume, "messageId": "small"},
                            {**resume, "cursor": ("A" if resume["cursor"][0] != "A" else "B") + resume["cursor"][1:]},
                            {**resume, "cursor": reader.token({**signed, "record": [0, 100000000, "0" * 64]})}):
                with self.assertRaises(ProtocolError) as raised:
                    read_segment(invalid, env)
                self.assertEqual("CURSOR_EXPIRED", raised.exception.code)
            for replacement in (original.replace("before", "BEFORE"), original.replace("hello", "olleh"), ""):
                self.file.write_text(replacement)
                with self.assertRaises(ProtocolError) as raised:
                    read_segment(resume, env)
                self.assertEqual("CURSOR_EXPIRED", raised.exception.code)
        self.file.write_text(original)
        self.index.write_text("{}")
        with self.assertRaises(ProtocolError) as raised:
            read_segment(resume, env)
        self.assertEqual("FORBIDDEN", raised.exception.code)

    def test_cli_resume_pins_projected_message_with_and_without_gateway_token(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        image = workspace / "chart.png"
        transcript = json.dumps({"type": "message", "id": "big", "message": {
            "role": "assistant", "content": "x" * 500000 + " MEDIA:" + str(image)}}) + "\n"
        self.file.write_text(transcript)
        for auth in ({"mode": "token", "token": "local-fixture-gateway-token"}, {}):
            with self.subTest(fast=bool(auth)):
                request = {**self.request, "action": "message", "messageId": "big",
                           "config": {"gateway": {"auth": auth}}}
                image.write_bytes(b"before")
                first = read_segment(request, {})
                cursor = first["nextCursor"]
                reader = SegmentReader(request, {})
                claims = reader.decode(cursor, "chunk")
                self.assertEqual("record" in claims, bool(auth))
                resume = {**request, "cursor": cursor}
                if auth:
                    with patch.object(SegmentReader, "scan", side_effect=AssertionError("unexpected full scan")):
                        second = read_segment(resume, {})
                else:
                    second = read_segment(resume, {})
                self.assertEqual(first["sha256"], second["sha256"])
                image.with_suffix(".new").write_bytes(b"after!")
                image.with_suffix(".new").replace(image)
                with self.assertRaises(ProtocolError) as raised:
                    read_segment({**request, "cursor": second["nextCursor"]}, {})
                self.assertEqual("CURSOR_EXPIRED", raised.exception.code)
                self.assertNotEqual(first["sha256"], read_segment(request, {})["sha256"])

    def test_cli_chunk_hints_preserve_branch_selection_and_file_identity(self):
        env = {"AGENT_MANAGER_HISTORY_CURSOR_KEY": "private-fixture-secret"}
        request = {**self.request, "action": "message", "messageId": "selected"}
        def message(i, parent):
            return {"type": "message", "id": i, "parentId": parent,
                    "message": {"role": "assistant", "content": i * 100000}}
        self.file.write_text("".join(json.dumps(row) + "\n" for row in (
            message("a", None), message("abandoned", "a"), message("selected", "a"))))
        with self.assertRaises(ProtocolError) as raised:
            read_segment({**request, "messageId": "abandoned"}, env)
        self.assertEqual("MESSAGE_NOT_FOUND", raised.exception.code)
        cursor = read_segment(request, env)["nextCursor"]
        clone = self.file.with_suffix(".replacement")
        clone.write_bytes(self.file.read_bytes())
        clone.replace(self.file)
        with self.assertRaises(ProtocolError) as raised:
            read_segment({**request, "cursor": cursor}, env)
        self.assertEqual("CURSOR_EXPIRED", raised.exception.code)

    def test_omit_thinking_inherits_pages_message_references_and_chunks(self):
        rows = [{"type": "message", "id": str(i), "parentId": str(i - 1) if i else None,
                 "message": {"role": "assistant", "content": [
                     {"type": "thinking", "thinking": "private thought" * 1000},
                     {"type": "text", "text": "answer" * 70000}]}} for i in range(2)]
        self.file.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        page = read_segment({**self.request, "omitThinking": True, "limit": 1}, os.environ)
        ref = page["messages"][0]["fullMessageRef"]["params"]
        request = {**self.request, **ref, "action": "message"}
        part = read_segment(request, os.environ)
        data = base64.b64decode(part["data"])
        while part["nextCursor"]:
            part = read_segment({**request, "cursor": part["nextCursor"]}, os.environ)
            data += base64.b64decode(part["data"])
        self.assertEqual(json.loads(data)["content"], [{"type": "text", "text": "answer" * 70000}])
        older = read_segment({**self.request, "cursor": page["olderCursor"]}, os.environ)
        token = older["messages"][0]["fullMessageRef"]["params"]["ref"]
        reader = SegmentReader(self.request, os.environ)
        self.assertTrue(reader.decode(token, "message")["omitThinking"])
        changed = read_segment({**self.request, "cursor": page["olderCursor"], "omitThinking": False}, os.environ)
        changed_ref = changed["messages"][0]["fullMessageRef"]["params"]["ref"]
        self.assertFalse(reader.decode(changed_ref, "message")["omitThinking"])
        self.assertIn("private thought", self.file.read_text())

    def test_page_cursor_survives_idle_but_child_refs_and_chunks_expire(self):
        rows = [{"type": "message", "id": str(i), "parentId": str(i - 1) if i else None,
                 "message": ({"role": "user", "content": [{"type": "image", "mimeType": "image/png", "data": "YWJj"}]}
                             if i % 2 else {"role": "assistant", "content": "answer" * 70000})} for i in range(4)]
        self.file.write_text("\n".join(map(json.dumps, rows)) + "\n")
        def read(**params):
            return read_segment({**self.request, "limit": 2, **params}, {})
        def reads_for(page):
            large, image = page["messages"]
            return ({"action": "message", **large["fullMessageRef"]["params"]},
                    {"action": "media", **image["mediaRefs"][0]["params"], "mediaOffset": 0, "mediaLength": 1})
        with patch("agent_manage.adapters.openclaw.v2026_7.segment_reader.time.time") as clock:
            clock.return_value = 2000000000
            page = read()
            original = reads_for(page)
            clock.return_value += 3500
            chunks = [read(**params)["nextCursor"] for params in original]
            self.assertTrue(all(chunks))
            clock.return_value += 200
            for params, chunk in zip(original, chunks):
                for request in (params, {**params, "cursor": chunk}):
                    with self.assertRaises(ProtocolError) as error:
                        read(**request)
                    self.assertEqual(error.exception.code, "CURSOR_EXPIRED")
            clock.return_value += 86400
            older = read(cursor=page["olderCursor"])
            self.assertEqual(older["messages"][0]["id"], "0")
            fresh = reads_for(older)
            for params in fresh:
                self.assertTrue(read(**params)["nextCursor"])
            clock.return_value += 86400
            self.assertEqual(read(cursor=older["newerCursor"])["messages"][0]["id"], "2")
            for params in fresh:
                with self.assertRaises(ProtocolError) as error:
                    read(**params)
                self.assertEqual(error.exception.code, "CURSOR_EXPIRED")
            self.file.write_text("")
            with self.assertRaises(ProtocolError) as error:
                read(cursor=page["olderCursor"])
            self.assertEqual(error.exception.code, "CURSOR_EXPIRED")

    def test_page_filter_can_change_and_new_cursor_inherits(self):
        rows = [{"type": "message", "id": str(i), "parentId": str(i-1) if i else None,
                 "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "thought"},
                                                               {"type": "text", "text": str(i)}]}}
                for i in range(4)]
        self.file.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        for initial in (False, True):
            page = read_segment({**self.request, "limit": 1, "omitThinking": initial}, os.environ)
            changed = read_segment({**self.request, "limit": 1, "cursor": page["olderCursor"],
                                    "omitThinking": not initial}, os.environ)
            inherited = read_segment({**self.request, "limit": 1, "cursor": changed["olderCursor"]}, os.environ)
            self.assertEqual([page["messages"][0]["id"], changed["messages"][0]["id"],
                              inherited["messages"][0]["id"]], ["3", "2", "1"])
            for result in (changed, inherited):
                self.assertEqual(any(b["type"] == "thinking" for b in result["messages"][0]["content"]), initial)

    def test_filtered_pages_fill_limit_in_both_directions(self):
        rows = []
        for i in range(12):
            content = [{"type": "text", "text": str(i)}] if i % 3 == 1 else [{"type": "thinking", "thinking": "hidden"}]
            rows.append({"type": "message", "id": str(i), "parentId": str(i-1) if i else None,
                         "message": {"role": "assistant", "content": content}})
        self.file.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        for view in ("compact", "full"):
            for edge, cursor_key, expected in (("start", "newerCursor", [["1", "4"], ["7", "10"]]),
                                               ("end", "olderCursor", [["7", "10"], ["1", "4"]])):
                with self.subTest(view=view, edge=edge):
                    request = {**self.request, "view": view, "omitThinking": True, "limit": 2, "edge": edge}
                    page = read_segment(request, os.environ)
                    if edge == "end":
                        self.assertEqual([m["id"] for m in page["messages"]], ["1", "4", "7", "10"])
                        self.assertIsNone(page["olderCursor"])
                        self.assertFalse(page["reachedUser"])
                        continue
                    self.assertEqual([m["id"] for m in page["messages"]], expected[0])
                    page = read_segment({**self.request, "limit": 2, "cursor": page[cursor_key]}, os.environ)
                    self.assertEqual([m["id"] for m in page["messages"]], expected[1])
                    if page[cursor_key]:
                        last = read_segment({**self.request, "limit": 2, "cursor": page[cursor_key]}, os.environ)
                        self.assertEqual(last["messages"], [])
                        self.assertIsNone(last[cursor_key])
        normal = read_segment({**self.request, "limit": 12}, os.environ)
        self.assertEqual(len(normal["messages"]), 12)

    def test_filtered_empty_segment_and_tool_media_records(self):
        messages = [
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "hidden"}]},
            {"role": "assistant", "content": [{"type": "text", "text": " "}]},
            {"role": "assistant", "content": [{"type": "toolCall", "id": "c", "name": "exec", "arguments": {"command": "pwd"}}]},
            {"role": "toolResult", "toolCallId": "c", "isError": False, "content": [{"type": "text", "text": "output"}]},
            {"role": "assistant", "content": [{"type": "image", "mimeType": "image/png", "data": base64.b64encode(b"image").decode()}]},
        ]
        rows = [{"type": "message", "id": str(i), "parentId": str(i-1) if i else None, "message": message}
                for i, message in enumerate(messages)]
        self.file.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        page = read_segment({**self.request, "omitThinking": True, "limit": 3}, os.environ)
        self.assertEqual([m["id"] for m in page["messages"]], ["2", "3", "4"])
        self.assertIn("toolSummary", page["messages"][1])
        self.assertIn("mediaRefs", page["messages"][2])
        self.file.write_text(json.dumps(rows[0]) + "\n")
        page = read_segment({**self.request, "omitThinking": True}, os.environ)
        self.assertEqual(page["messages"], [])
        self.assertIsNone(page["olderCursor"])
        self.assertIsNone(page["newerCursor"])

    def test_omit_thinking_preserves_answer_tools_and_default(self):
        content = [{"type": "thinking", "thinking": "thought"},
                   {"type": "text", "text": "answer"},
                   {"type": "toolCall", "id": "c", "name": "exec", "arguments": {"command": "pwd"}}]
        self.file.write_text(json.dumps({"type": "message", "id": "a", "message": {
            "role": "assistant", "content": content}}) + "\n")
        for view in ("compact", "full"):
            normal = read_segment({**self.request, "view": view}, os.environ)
            self.assertEqual(normal["messages"][0]["content"][0]["type"], "thinking")
            filtered = read_segment({**self.request, "view": view, "omitThinking": True}, os.environ)
            self.assertEqual([b["type"] for b in filtered["messages"][0]["content"]], ["text", "toolCall"])

    def test_two_hundred_limit_with_filtering_and_bidirectional_pages(self):
        rows = []
        for i in range(850):
            content = [{"type": "thinking", "thinking": "hidden"}] if i % 2 else [{"type": "text", "text": "回复 " + str(i)}]
            rows.append({"type": "message", "id": str(i), "parentId": str(i-1) if i else None,
                         "message": {"role": "assistant", "content": content}})
        self.file.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        for edge, direction in (("start", "newerCursor"), ("end", "olderCursor")):
            page = read_segment({**self.request, "edge": edge, "limit": 200, "omitThinking": True}, os.environ)
            seen, sizes = [], []
            while True:
                ids = [m["id"] for m in page["messages"]]
                sizes.append(len(ids))
                seen = seen + ids if edge == "start" else ids + seen
                if not page[direction]:
                    break
                page = read_segment({**self.request, "limit": 200, "cursor": page[direction]}, os.environ)
            self.assertEqual(sizes, [200, 200, 25] if edge == "start" else [425])
            self.assertEqual(seen, [str(i) for i in range(0, 850, 2)])

    def test_unicode_multiline_page_stays_under_final_response_budget(self):
        # UTF-8 and enclosing JSON indentation cost more than character counts.
        text = "中文\n" * 18000
        rows = [{"type": "message", "id": str(i), "parentId": str(i-1) if i else None,
                 "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}
                for i in range(20)]
        self.file.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n")
        page = read_segment({**self.request, "limit": 200}, os.environ)
        self.assertGreater(len(page["messages"]), 2)
        self.assertLess(len(page["messages"]), 20)
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            print_json(build_success_response(page))
        self.assertIsNone(json.loads(output.getvalue())["error"])
        self.assertLess(len(output.getvalue().encode()), 8 * 1024 * 1024)
        self.assertEqual(page["messages"][0]["content"][0]["text"], text)

    def test_default_backward_reads_to_user_beyond_limit_and_continues(self):
        rows = [{"type": "message", "id": str(i), "parentId": str(i-1) if i else None,
                 "message": {"role": "user" if i in (0, 3) else "assistant",
                             "content": [{"type": "text", "text": str(i)}]}} for i in range(260)]
        self.file.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        page = read_segment({**self.request, "limit": 2}, os.environ)
        self.assertEqual([m["id"] for m in page["messages"]], [str(i) for i in range(3, 260)])
        self.assertTrue(page["reachedUser"])
        older = read_segment({**self.request, "cursor": page["olderCursor"]}, os.environ)
        self.assertEqual([m["id"] for m in older["messages"]], ["0", "1", "2"])
        self.assertTrue(older["reachedUser"])
        self.assertIsNone(older["olderCursor"])
        preview = read_segment({**self.request, "limit": 1}, os.environ)
        self.assertEqual([m["id"] for m in preview["messages"]], ["259"])
        self.assertFalse(preview["reachedUser"])

    def test_user_inside_limit_does_not_stop_early_or_extend_to_another_user(self):
        rows = [{"type": "message", "id": str(i), "parentId": str(i-1) if i else None,
                 "message": {"role": "user" if i in (0, 3, 7) else "assistant",
                             "content": [{"type": "text", "text": str(i)}]}} for i in range(10)]
        self.file.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        page = read_segment({**self.request, "limit": 5}, os.environ)
        # User 7 is already included: fill to 5, do not stop at 7 or extend to 3.
        self.assertEqual([m["id"] for m in page["messages"]], ["5", "6", "7", "8", "9"])
        self.assertTrue(page["reachedUser"])
        previous = read_segment({**self.request, "limit": 5, "cursor": page["olderCursor"]}, os.environ)
        self.assertEqual([m["id"] for m in previous["messages"]], ["0", "1", "2", "3", "4"])
        self.assertIsNone(previous["olderCursor"])

    def test_backward_user_boundary_respects_byte_budget(self):
        rows = [{"type": "message", "id": str(i), "parentId": str(i-1) if i else None,
                 "message": {"role": "user" if i == 0 else "assistant",
                             "content": [{"type": "text", "text": "x" * 100000}]}} for i in range(15)]
        self.file.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        page = read_segment({**self.request, "limit": 2}, os.environ)
        self.assertEqual(len(page["messages"]), 10)
        self.assertFalse(page["reachedUser"])
        older = read_segment({**self.request, "cursor": page["olderCursor"], "limit": 2}, os.environ)
        self.assertTrue(older["reachedUser"])
        self.assertIsNone(older["olderCursor"])
        self.assertEqual([m["id"] for m in older["messages"] + page["messages"]], [str(i) for i in range(15)])

    def test_larger_page_budget_and_serialized_output(self):
        rows = [{"type": "message", "id": str(i), "parentId": str(i-1) if i else None,
                 "message": {"role": "assistant", "content": [{"type": "text", "text": "x" * 100000}]}}
                for i in range(15)]
        self.file.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        page = read_segment({**self.request, "limit": 200}, os.environ)
        self.assertEqual(len(page["messages"]), 10)
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            print_json(build_success_response(page))
        self.assertIsNone(json.loads(output.getvalue())["error"])
        previous = read_segment({**self.request, "cursor": page["olderCursor"], "limit": 200}, os.environ)
        self.assertEqual([m["id"] for m in previous["messages"] + page["messages"]], [str(i) for i in range(15)])

    def test_default_omits_code_and_does_not_fetch_original(self):
        with patch("subprocess.run", side_effect=AssertionError("No child processes")):
            page = read_segment(self.request, os.environ)
        self.assertEqual(page["view"], "compact")
        tool = page["messages"][0]
        self.assertEqual(tool["content"], [])
        self.assertEqual(tool["toolSummary"], {"name": "read", "status": "success"})
        self.assertNotIn("fullMessageRef", tool)
        self.assertLess(len(json.dumps(page)), 1000)
        full = read_segment({**self.request, "view": "full"}, os.environ)
        self.assertEqual(full["messages"][0]["content"][0]["text"], "source code " * 10000)

    def test_index_and_membership_fail_without_falling_back(self):
        for index, code in (({}, "FORBIDDEN"), ({self.key.lower(): {"sessionId": "physical"}}, "FORBIDDEN"),
                            ({self.key: {"sessionId": "another", "usageFamilySessionIds": []}}, "FORBIDDEN")):
            self.index.write_text(json.dumps(index))
            with self.assertRaises(ProtocolError) as error:
                read_segment(self.request, os.environ)
            self.assertEqual(error.exception.code, code)
        self.index.write_text("not-json")
        with self.assertRaises(ProtocolError) as error:
            read_segment(self.request, os.environ)
        self.assertEqual(error.exception.code, "SEGMENT_READ_FAILED")

    def test_long_thinking_chunk_does_not_restore_omitted_tool_arguments(self):
        thought = "Stored thinking " * 30000
        self.file.write_text(json.dumps({"type": "message", "id": "assistant", "parentId": None,
            "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": thought},
            {"type": "toolCall", "id": "call", "name": "exec", "arguments": {"command": "echo ready", "code": "hidden original code" * 20000}}]}}) + "\n")
        page = read_segment(self.request, os.environ)
        ref = page["messages"][0]["fullMessageRef"]["params"]["ref"]
        request = {**self.request, "action": "message", "messageId": "assistant", "ref": ref}
        part = read_segment(request, os.environ)
        reader = SegmentReader(self.request, os.environ)
        legacy = reader.decode(part["nextCursor"], "chunk")
        legacy.pop("format")
        with self.assertRaises(ProtocolError) as error:
            read_segment({**request, "cursor": reader.token(legacy)}, os.environ)
        self.assertEqual(error.exception.code, "CURSOR_EXPIRED")
        data = b""
        while True:
            self.assertEqual(part["view"], "compact")
            data += base64.b64decode(part["data"])
            if not part["nextCursor"]:
                break
            part = read_segment({**request, "cursor": part["nextCursor"]}, os.environ)
        message = json.loads(data)
        self.assertEqual(message["content"][0]["thinking"], thought)
        self.assertEqual(message["content"][1]["arguments"], {"command": "echo ready"})
        self.assertNotIn("hidden original code", data.decode())

    def test_empty_message_and_zero_timestamp_are_retained(self):
        self.file.write_text(json.dumps({"type": "message", "id": "empty", "parentId": None, "timestamp": 0, "message": {}}) + "\n")
        page = read_segment(self.request, os.environ)
        self.assertEqual(page["messages"][0]["id"], "empty")
        self.assertEqual(page["messages"][0]["timestamp"], 0)
        self.assertEqual(page["segment"]["startedAt"], 0)

    def test_thinking_signature_removed_from_all_display_reads_only(self):
        signature = json.dumps({"id": "rs-fixture", "type": "reasoning", "encrypted_content": "encrypted" * 50000})
        record = {"type": "message", "id": "thought", "parentId": None,
                  "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "Readable thought",
                              "thinkingSignature": signature}], "thinkingSignature": signature}}
        self.file.write_text(json.dumps(record) + "\n")
        before = self.file.read_bytes()
        for view in ("compact", "full"):
            page = read_segment({**self.request, "view": view}, os.environ)
            message = page["messages"][0]
            self.assertEqual(message["content"][0], {"type": "thinking", "thinking": "Readable thought"})
            self.assertNotIn("thinkingSignature", message)
            self.assertNotIn("fullMessageRef", message)
            self.assertLess(len(json.dumps(page)), 1000)
        fetched = read_segment({**self.request, "action": "message", "messageId": "thought"}, os.environ)
        self.assertEqual(fetched["message"]["content"][0]["thinking"], "Readable thought")
        self.assertNotIn("thinkingSignature", json.dumps(fetched))
        self.assertEqual(self.file.read_bytes(), before)

    def test_compact_arguments_remain_compatible_with_existing_ui(self):
        calls = [
            {"type": "toolCall", "id": "exec", "name": "exec", "arguments": {"command": "rg\n auth " + "x" * 1000, "code": "omit"}},
            {"type": "toolCall", "id": "read", "name": "read", "arguments": {"path": "/app/extensions/openai/speech-providers.ts", "offset": 318, "limit": 500}},
            {"type": "tool_use", "id": "query", "name": "search", "input": {"query": "search text", "large": "omit"}},
            {"type": "toolCall", "id": "encoded", "name": "exec", "arguments": json.dumps({"cmd": "echo ready", "code": "omit"})},
        ]
        self.file.write_text(json.dumps({"type": "message", "id": "calls", "parentId": None,
                                        "message": {"role": "assistant", "content": calls}}) + "\n")
        blocks = read_segment(self.request, os.environ)["messages"][0]["content"]
        keys = ("command", "cmd", "path", "file_path", "filePath", "url", "query", "pattern")
        for block in blocks:
            # The current frontend derives preview from these original argument
            # fields, without reading the newly introduced summary field.
            preview = next((block["arguments"].get(key) for key in keys if isinstance(block["arguments"].get(key), str)), "")
            self.assertTrue(preview)
            self.assertLessEqual(len(preview), 180)
            self.assertNotIn("\n", preview)
            self.assertEqual(preview, block["summary"])
            self.assertNotIn("code", block["arguments"])
        self.assertEqual(blocks[2]["input"], {"query": "search text"})


@unittest.skipUnless(os.environ.get("AGENT_MANAGE_TEST_OPENCLAW_BIN"), "Set July OpenClaw for native history tests")
class NativeSegmentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="agent-segment-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.binary = os.environ["AGENT_MANAGE_TEST_OPENCLAW_BIN"]
        self.package = Path(self.binary).resolve().parent
        self.key = "agent:example:dashboard:thread-key"
        self.ids = [f"00000000-0000-4000-8000-{number:012}" for number in (1, 2, 3)]
        self.sessions = self.root / "agents/example/sessions"
        self.sessions.mkdir(parents=True)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.config = self.root / "openclaw.json"
        self.config.write_text(json.dumps({"gateway": {"auth": {"mode": "token", "token": "fixture-only-secret"}},
            "agents": {"list": [{"id": "example", "workspace": str(self.workspace)}]}}))
        self.entry = {"sessionId": self.ids[-1], "usageFamilySessionIds": self.ids,
                      "updatedAt": 1791700000000, "displayName": "Keep this title", "model": "fixture-model"}
        self.seed()
        self.paths = []
        for index, session_id in enumerate(self.ids):
            file = self.sessions / (session_id + ".jsonl" + (".reset.2026-10-12T02-00-00.000Z" if index < 2 else ""))
            records = [{"type": "session", "id": session_id, "version": 3, "timestamp": "2026-10-12T01:00:00.000Z"}]
            records += [self.record(f"s{index}-{n}", "user" if n % 2 == 0 else "assistant", "正文" * 1800 if n == 0 else f"message-{n}",
                                    None if n == 0 else f"s{index}-{n-1}") for n in range(7)]
            file.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))
            self.paths.append(file)
        self.manager = InstanceManagerV2(LocalRunner(self.binary), config_path=str(self.config))

    def seed(self):
        script = """const sdk=await import(process.argv[1]+'/dist/plugin-sdk/session-store-runtime.js');
await sdk.saveSessionStore(process.argv[2], JSON.parse(process.argv[3]), {skipMaintenance:true});"""
        self.env = dict(os.environ, OPENCLAW_STATE_DIR=str(self.root), OPENCLAW_CONFIG_PATH=str(self.config))
        subprocess.run(["node", "--input-type=module", "-e", script, str(self.package),
                        str(self.sessions / "sessions.json"), json.dumps({self.key: self.entry})],
                       env=self.env, capture_output=True, check=True)

    @staticmethod
    def record(id, role, text, parent=None):
        return {"type": "message", "id": id, "parentId": parent, "timestamp": "2026-10-12T01:00:01.000Z",
                "message": {"role": role, "content": [{"type": "text", "text": text}], "timestamp": 1791766801000}}

    def append(self, record, index=0):
        with self.paths[index].open("a") as stream:
            stream.write(json.dumps(record) + "\n")

    def history(self, index=0, **options):
        options.setdefault("view", "full")
        return self.manager.segment_history(self.key, self.ids[index], **options)

    def test_three_segments_bidirectional_full_text_and_read_only(self):
        before = {file: file.read_bytes() for file in [self.config, *self.paths]}
        # Compare both July JSON session indexes and any native SQLite stores byte-for-byte.
        before.update({file: file.read_bytes() for file in self.root.rglob("sessions.json")})
        database = list(self.root.rglob("*.sqlite"))
        before.update({file: file.read_bytes() for file in database})
        for index in range(3):
            page = self.history(index, edge="start", limit=2)
            self.assertEqual(page["messages"][0]["content"][0]["text"], "正文" * 1800)
            self.assertEqual(page["segment"]["isCurrent"], index == 2)
            self.assertLessEqual(len(page["segment"]["preview"][0]), 180)
            forward = list(page["messages"])
            while page["newerCursor"]:
                page = self.history(index, cursor=page["newerCursor"], limit=2)
                forward.extend(page["messages"])
            self.assertEqual([m["id"] for m in forward], [f"s{index}-{n}" for n in range(7)])
            page = self.history(index, edge="end", limit=3)
            backward = list(page["messages"])
            while page["olderCursor"]:
                page = self.history(index, cursor=page["olderCursor"], limit=3)
                backward = page["messages"] + backward
            self.assertEqual([m["id"] for m in backward], [m["id"] for m in forward])
        for file, content in before.items():
            self.assertEqual(file.read_bytes(), content, str(file))

    def test_tools_thinking_images_and_protected_media_chunks(self):
        image = self.workspace / "image.png"
        image.write_bytes(b"fixture-image" * 20000)
        record = self.record("tools", "assistant", "MEDIA:" + str(image), "s0-6")
        record["message"]["content"] += [{"type": "thinking", "thinking": "Saved reasoning"},
          {"type": "toolCall", "id": "call1", "name": "draw", "arguments": {"prompt": "完整参数"}},
          {"type": "image", "mimeType": "image/png", "data": base64.b64encode(b"inline-image").decode()}]
        self.append(record)
        result = self.record("result", "toolResult", "tool-result" * 2000, "tools")
        result["message"]["toolCallId"] = "call1"
        self.append(result)
        page = self.history(limit=2)
        message = page["messages"][-2]
        self.assertEqual(message["content"][1]["thinking"], "Saved reasoning")
        self.assertEqual(message["content"][2]["arguments"], {"prompt": "完整参数"})
        self.assertEqual(page["messages"][-1]["content"][0]["text"], "tool-result" * 2000)
        for reference, expected in zip(message["mediaRefs"], (image.read_bytes(), b"inline-image")):
            params = reference["params"]
            part = self.manager.segment_media_get(self.key, self.ids[0], params["ref"])
            content = base64.b64decode(part["data"])
            self.assertIsNone(part["nextCursor"])
            while part["nextCursor"]:
                part = self.manager.segment_media_get(self.key, self.ids[0], cursor=part["nextCursor"])
                content += base64.b64decode(part["data"])
            self.assertEqual(content, expected)

    def test_saved_media_metadata_relative_paths_and_file_urls(self):
        image = self.workspace / "image with spaces.png"
        image.write_bytes(b"image-with-space")
        record = self.record("metadata", "user", "Image", "s0-6")
        record["message"]["media"] = [{"path": "image with spaces.png", "contentType": "image/png"}]
        record["message"]["details"] = {"mediaUrls": [image.as_uri()]}
        self.append(record)
        message = self.history(limit=1)["messages"][0]
        self.assertEqual(message["media"], record["message"]["media"])
        for ref in message["mediaRefs"]:
            result = self.manager.segment_media_get(self.key, self.ids[0], ref["params"]["ref"])
            self.assertEqual(base64.b64decode(result["data"]), image.read_bytes())

    def test_same_image_inline_text_and_metadata_have_one_identity(self):
        image = self.workspace / "same.png"
        image.write_bytes(b"same-image")
        encoded = base64.b64encode(image.read_bytes()).decode()
        record = self.record("duplicate", "assistant", "MEDIA:" + str(image), "s0-6")
        record["message"]["content"] += [
            {"type": "image", "mimeType": "image/png", "data": encoded},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": encoded}},
        ]
        record["message"]["MediaPath"] = str(image)
        record["message"]["media"] = [{"path": "same.png"}, {"url": image.as_uri()}]
        self.append(record)
        message = self.history(limit=1)["messages"][0]
        self.assertEqual(len(message["mediaRefs"]), 1)
        expected_id = "sha256:" + hashlib.sha256(image.read_bytes()).hexdigest()
        self.assertEqual(message["mediaRefs"][0]["mediaId"], expected_id)
        self.assertEqual(message["content"][1]["mediaRef"]["mediaId"], expected_id)
        self.assertEqual(message["content"][2]["source"]["mediaRef"]["mediaId"], expected_id)
        params = message["mediaRefs"][0]["params"]
        media = self.manager.segment_media_get(self.key, self.ids[0], params["ref"])
        self.assertEqual(media["sha256"], expected_id.removeprefix("sha256:"))
        self.assertEqual(base64.b64decode(media["data"]), image.read_bytes())

    def test_different_images_and_thumbnail_remain_separate(self):
        record = self.record("distinct", "assistant", "Images", "s0-6")
        contents = [b"original-image", b"thumbnail-image", b"different-image"]
        record["message"]["content"] += [{"type": "image", "mimeType": "image/png",
            "data": base64.b64encode(value).decode()} for value in contents]
        self.append(record)
        refs = self.history(limit=1)["messages"][0]["mediaRefs"]
        self.assertEqual([ref["mediaId"] for ref in refs], ["sha256:" + hashlib.sha256(value).hexdigest() for value in contents])

    def test_unreadable_media_retains_references_without_identity(self):
        image = self.workspace / "good.png"
        image.write_bytes(b"same-bytes")
        outside = self.root / "outside.png"
        outside.write_bytes(image.read_bytes())
        link = self.workspace / "link.png"
        link.symlink_to(image)
        paths = [image, self.workspace / "missing.png", self.workspace / "other-missing.png", outside, link]
        record = self.record("unreadable", "assistant", "Keep this message", "s0-6")
        record["message"]["media"] = [{"path": str(file)} for file in paths]
        record["message"]["content"].append({"type": "image", "data": "not-valid-base64!"})
        self.append(record)
        message = self.history(limit=1)["messages"][0]
        self.assertEqual(message["content"][0]["text"], "Keep this message")
        refs = message["mediaRefs"]
        self.assertEqual(len(refs), 6)
        # Inline invalid data is visited first; only the allowed existing file gets an ID.
        self.assertEqual(sum("mediaId" in ref for ref in refs), 1)
        for ref, code in zip(refs[2:], ["MEDIA_NOT_FOUND", "MEDIA_NOT_FOUND", "FORBIDDEN", "FORBIDDEN"]):
            with self.assertRaises(ProtocolError) as error:
                self.manager.segment_media_get(self.key, self.ids[0], ref["params"]["ref"])
            self.assertEqual(error.exception.code, code)

    def test_media_identity_detects_change_before_first_download(self):
        image = self.workspace / "changed.png"
        image.write_bytes(b"original")
        self.append(self.record("changed", "assistant", "MEDIA:" + str(image), "s0-6"))
        ref = self.history(limit=1)["messages"][0]["mediaRefs"][0]
        image.write_bytes(b"replacement")
        with self.assertRaises(ProtocolError) as error:
            self.manager.segment_media_get(self.key, self.ids[0], ref["params"]["ref"])
        self.assertEqual(error.exception.code, "CURSOR_EXPIRED")

    def test_repeated_local_file_is_hashed_once_per_history_read(self):
        image = self.workspace / "cached.png"
        image.write_bytes(b"cached-image" * 20000)
        for n in range(2):
            record = self.record(f"cached-{n}", "assistant", "MEDIA:" + str(image), "s0-6" if n == 0 else "cached-0")
            record["message"]["media"] = [{"path": "cached.png"}, {"url": image.as_uri()}]
            self.append(record)
        original_read = os.read
        read_bytes = 0

        def observed_read(fd, size):
            nonlocal read_bytes
            data = original_read(fd, size)
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) == (image.stat().st_dev, image.stat().st_ino):
                read_bytes += len(data)
            return data

        with patch("agent_manage.adapters.openclaw.v2026_7.segment_reader.os.read", side_effect=observed_read):
            messages = self.history(limit=2)["messages"][-2:]
        self.assertEqual(read_bytes, image.stat().st_size)
        self.assertEqual([len(message["mediaRefs"]) for message in messages], [1, 1])
        self.assertEqual(messages[0]["mediaRefs"][0]["mediaId"], messages[1]["mediaRefs"][0]["mediaId"])

    def test_media_symlink_missing_and_changed_chunk(self):
        target = self.workspace / "target.png"
        target.write_bytes(b"target")
        link = self.workspace / "link.png"
        link.symlink_to(target)
        self.append(self.record("linked", "assistant", "MEDIA:" + str(link), "s0-6"))
        ref = self.history(limit=1)["messages"][0]["mediaRefs"][0]["params"]["ref"]
        with self.assertRaises(ProtocolError) as error:
            self.manager.segment_media_get(self.key, self.ids[0], ref)
        self.assertEqual(error.exception.code, "FORBIDDEN")
        self.append(self.record("target", "assistant", "MEDIA:" + str(target), "linked"))
        target.write_bytes(b"a" * (5 * 1024 * 1024 + 1))
        ref = self.history(limit=1)["messages"][0]["mediaRefs"][0]["params"]["ref"]
        part = self.manager.segment_media_get(self.key, self.ids[0], ref)
        self.assertEqual(len(base64.b64decode(part["data"])), 5 * 1024 * 1024)
        self.assertIsNotNone(part["nextCursor"])
        last = self.manager.segment_media_get(self.key, self.ids[0], cursor=part["nextCursor"])
        self.assertEqual(base64.b64decode(last["data"]), b"a")
        self.assertIsNone(last["nextCursor"])
        target.write_bytes(b"b" * (5 * 1024 * 1024 + 1))
        with self.assertRaises(ProtocolError) as error:
            self.manager.segment_media_get(self.key, self.ids[0], ref, cursor=part["nextCursor"])
        self.assertEqual(error.exception.code, "CURSOR_EXPIRED")
        target.unlink()
        with self.assertRaises(ProtocolError) as error:
            self.manager.segment_media_get(self.key, self.ids[0], ref)
        self.assertEqual(error.exception.code, "MEDIA_NOT_FOUND")

    def test_oversized_message_can_be_reassembled_within_output_budget(self):
        text = "完整长正文🙂" * 70000
        self.append(self.record("large", "assistant", text, "s0-6"))
        page = self.history(limit=1)
        message = page["messages"][0]
        self.assertTrue(message["__openclaw"]["truncated"])
        ref = message["fullMessageRef"]["params"]["ref"]
        part = self.manager.segment_message_get(self.key, self.ids[0], "large", ref=ref)
        content = b""
        while True:
            output = io.StringIO()
            with redirect_stdout(output):
                print_json(build_success_response(part))
            self.assertIsNone(json.loads(output.getvalue())["error"])
            content += base64.b64decode(part["data"])
            if not part["nextCursor"]:
                break
            part = self.manager.segment_message_get(self.key, self.ids[0], "large", cursor=part["nextCursor"])
        self.assertEqual(json.loads(content)["content"][0]["text"], text)

    def test_snapshot_survives_append_and_reset_rename(self):
        page = self.history(2, edge="start", limit=2)
        self.append(self.record("new", "assistant", "new message", "s2-6"), 2)
        self.paths[2].rename(self.paths[2].with_name(self.paths[2].name + ".reset.2026-10-12T03-00-00.000Z"))
        new_id = "00000000-0000-4000-8000-000000000004"
        self.entry.update(sessionId=new_id, usageFamilySessionIds=[*self.ids, new_id])
        self.seed()
        messages = list(page["messages"])
        while page["newerCursor"]:
            page = self.history(2, cursor=page["newerCursor"], limit=2)
            messages.extend(page["messages"])
        self.assertEqual([m["id"] for m in messages], [f"s2-{n}" for n in range(7)])
        self.assertFalse(page["segment"]["isCurrent"])

    def test_forbidden_missing_expired_and_empty_are_distinct(self):
        with self.assertRaises(ProtocolError) as error:
            self.manager.segment_history(self.key, "other-thread-id")
        self.assertEqual(error.exception.code, "FORBIDDEN")
        cursor = self.history(limit=2)["olderCursor"]
        with self.assertRaises(ProtocolError) as error:
            self.history(1, cursor=cursor)
        self.assertEqual(error.exception.code, "CURSOR_EXPIRED")
        self.paths[0].unlink()
        with self.assertRaises(ProtocolError) as error:
            self.history()
        self.assertEqual(error.exception.code, "SEGMENT_NOT_FOUND")
        with self.assertRaises(ProtocolError) as error:
            self.history(cursor=cursor)
        self.assertEqual(error.exception.code, "CURSOR_EXPIRED")
        self.paths[0].write_text(json.dumps({"type": "session", "id": self.ids[0]}) + "\n")
        empty = self.history()
        self.assertEqual(empty["messages"], [])
        self.assertIsNone(empty["olderCursor"])
        self.assertIsNone(empty["newerCursor"])

    def test_rewritten_snapshot_and_media_escape_rejected(self):
        cursor = self.history(limit=2)["olderCursor"]
        self.paths[0].write_text(self.paths[0].read_text().replace("message-1", "rewritten"))
        with self.assertRaises(ProtocolError) as error:
            self.history(cursor=cursor)
        self.assertEqual(error.exception.code, "CURSOR_EXPIRED")
        outside = self.root / "private.png"
        outside.write_bytes(b"not-media-root")
        self.append(self.record("escape", "assistant", "MEDIA:" + str(outside), "s0-6"))
        ref = self.history(limit=1)["messages"][0]["mediaRefs"][0]["params"]["ref"]
        with self.assertRaises(ProtocolError) as error:
            self.manager.segment_media_get(self.key, self.ids[0], ref)
        self.assertEqual(error.exception.code, "FORBIDDEN")

    def test_native_branch_selection_and_partial_tail(self):
        self.append(self.record("branch-a", "assistant", "selected", "s2-6"), 2)
        self.append(self.record("branch-b", "assistant", "abandoned", "s2-6"), 2)
        self.append({"type": "leaf", "id": "selector", "parentId": "branch-b", "targetId": "branch-a"}, 2)
        with self.paths[2].open("a") as stream:
            stream.write('{"type":"message"')
        messages = self.history(2, limit=30)["messages"]
        self.assertIn("branch-a", [m["id"] for m in messages])
        self.assertNotIn("branch-b", [m["id"] for m in messages])

    def test_archive_without_newline_preserves_last_record(self):
        self.paths[0].write_text(self.paths[0].read_text().rstrip("\n"))
        page = self.history(edge="end", limit=1)
        self.assertEqual(page["messages"][0]["id"], "s0-6")
        page = self.history(cursor=page["olderCursor"], limit=1)
        self.assertEqual(page["messages"][0]["id"], "s0-5")

    def test_default_compact_tools_preserves_thinking_text_and_media(self):
        command = "grep -n 'auth' /app/extensions/openai/speech-providers.ts; " + "generated code " * 50000
        call = self.record("compact-call", "assistant", "I checked the authentication implementation", "s0-6")
        call["message"]["content"] += [
            {"type": "thinking", "thinking": "Saved thinking stays intact"},
            {"type": "toolCall", "id": "call-compact", "name": "exec", "arguments": {"command": command, "code": "source" * 80000}},
        ]
        self.append(call)
        result = self.record("compact-result", "toolResult", "source code output" * 50000, "compact-call")
        image_bytes = b"tool-image"
        result["message"].update(toolName="exec", toolCallId="call-compact", isError=False, details={"stdout": command})
        result["message"]["content"].append({"type": "image", "mimeType": "image/png", "data": base64.b64encode(image_bytes).decode()})
        self.append(result)
        page = self.manager.segment_history(self.key, self.ids[0], limit=2)
        self.assertEqual(page["view"], "compact")
        self.assertLess(len(json.dumps(page).encode()), 6000)
        assistant, tool = page["messages"][-2:]
        self.assertEqual(assistant["content"][0]["text"], call["message"]["content"][0]["text"])
        self.assertEqual(assistant["content"][1], call["message"]["content"][1])
        summary = assistant["content"][2]
        self.assertEqual(summary["arguments"], {"command": summary["summary"]})
        self.assertLessEqual(len(summary["summary"]), 180)
        self.assertTrue(summary["summary"].startswith("grep"))
        self.assertEqual(tool["toolSummary"], {"name": "exec", "status": "success"})
        self.assertEqual(tool["toolCallId"], "call-compact")
        self.assertNotIn("details", tool)
        self.assertTrue(all("fullMessageRef" not in message for message in page["messages"]))
        self.assertEqual(tool["content"][0]["type"], "image")
        media = self.manager.segment_media_get(self.key, self.ids[0], tool["mediaRefs"][0]["params"]["ref"])
        self.assertEqual(base64.b64decode(media["data"]), image_bytes)

    def test_compact_cursor_keeps_view_and_advances_over_empty_tool_results(self):
        for n in range(4):
            record = self.record(f"tool-{n}", "toolResult", "long output" * 100, "s0-6" if n == 0 else f"tool-{n-1}")
            record["message"].update(toolName="read", isError=n == 1)
            self.append(record)
        page = self.manager.segment_history(self.key, self.ids[0], limit=2)
        cursor = page["olderCursor"]
        self.assertEqual([m["id"] for m in page["messages"]], ["s0-6", "tool-0", "tool-1", "tool-2", "tool-3"])
        self.assertEqual(page["messages"][2]["toolSummary"]["status"], "error")
        page = self.manager.segment_history(self.key, self.ids[0], limit=2, cursor=cursor)
        self.assertEqual(page["view"], "compact")
        self.assertEqual([m["id"] for m in page["messages"]], ["s0-4", "s0-5"])
        with self.assertRaises(ProtocolError) as error:
            self.manager.segment_history(self.key, self.ids[0], cursor=cursor, view="full")
        self.assertEqual(error.exception.code, "CURSOR_EXPIRED")

    def test_reader_starts_no_process_and_uses_topic_and_custom_store(self):
        self.key = "agent:example:dashboard:thread-key:topic:123"
        self.sessions = self.root / "custom/example/history"
        self.sessions.mkdir(parents=True)
        config = json.loads(self.config.read_text())
        config["session"] = {"store": str(self.root / "custom/{agentId}/history/sessions.json")}
        self.config.write_text(json.dumps(config))
        self.seed()
        file = self.sessions / (self.ids[0] + "-topic-123.jsonl.reset.2026-10-12T02-00-00.000Z")
        file.write_bytes(self.paths[0].read_bytes())
        with patch("subprocess.run", side_effect=AssertionError("reader must not start processes")):
            page = self.manager.segment_history(self.key, self.ids[0], limit=1)
        self.assertEqual(page["messages"][0]["id"], "s0-6")
        self.assertIsInstance(page["timings"]["hostReadMs"], (int, float))
        self.assertGreaterEqual(page["timings"]["hostReadMs"], 0)
        phases = page["timings"]["phasesMs"]
        for name in ("environmentAndVersionMs", "configReadMs", "readerInitMs", "indexReadMs",
                     "fileLocateAndOpenMs", "transcriptScanMs", "branchSelectionMs", "messageBuildMs",
                     "messageReadMs", "snapshotVerificationMs"):
            self.assertGreaterEqual(phases[name], 0, name)
        self.assertEqual(page["timings"]["phaseCalls"]["indexReadMs"], 2)
        self.assertEqual(page["timings"]["counts"]["messagesReturned"], 1)
        self.assertEqual(page["timings"]["counts"]["recordsScanned"], 8)
        self.assertEqual(page["timings"]["counts"]["transcriptBytes"], file.stat().st_size)

    def test_transcript_index_symlinks_and_header_mismatch_rejected(self):
        saved = self.paths[0].read_bytes()
        self.paths[0].unlink()
        target = self.root / "private-transcript.jsonl"
        target.write_bytes(saved)
        self.paths[0].symlink_to(target)
        with self.assertRaises(ProtocolError) as error:
            self.history()
        # Reset discovery ignores symlinks, like July, so it is not an archive.
        self.assertEqual(error.exception.code, "SEGMENT_NOT_FOUND")
        self.paths[0].unlink()
        active = self.sessions / (self.ids[0] + ".jsonl")
        active.symlink_to(target)
        with self.assertRaises(ProtocolError) as error:
            self.history()
        self.assertEqual(error.exception.code, "FORBIDDEN")

        active.unlink()
        active.write_bytes(saved.replace(self.ids[0].encode(), self.ids[1].encode(), 1))
        with self.assertRaises(ProtocolError) as error:
            self.history()
        self.assertEqual(error.exception.code, "FORBIDDEN")
        index = self.sessions / "sessions.json"
        index_target = self.root / "index.json"
        index.rename(index_target)
        index.symlink_to(index_target)
        with self.assertRaises(ProtocolError) as error:
            self.history()
        self.assertEqual(error.exception.code, "FORBIDDEN")

    def test_branch_selection_matches_july_native_algorithm(self):
        cases = [
            [{"type": "message", "id": "a"}, {"type": "message", "id": "b"}],
            [{"type": "message", "id": "a", "parentId": None},
             {"type": "message", "id": "side", "parentId": "a", "appendMode": "side"},
             {"type": "message", "id": "b", "parentId": "side"}],
            [{"type": "message", "id": "a", "parentId": None},
             {"type": "message", "id": "b", "parentId": "a"},
             {"type": "message", "id": "c", "parentId": "a"},
             {"type": "leaf", "id": "leaf", "parentId": "c", "targetId": "b"},
             {"type": "message", "id": "d", "parentId": "leaf"}],
            [{"type": "message", "id": "a", "parentId": "missing"},
             {"type": "message", "id": "b"},
             {"type": "leaf", "id": "leaf", "parentId": "b", "targetId": None}],
        ]
        script = """import fs from 'node:fs';
const dir=process.argv[1]+'/dist';
const file=fs.readdirSync(dir).find(n=>n.startsWith('transcript-tree-')&&n.endsWith('.js'));
const source=fs.readFileSync(dir+'/'+file,'utf8'), mod=await import(dir+'/'+file);
const scan=mod[source.match(/scanSessionTranscriptTree as (\\w+)/)[1]];
const select=mod[source.match(/selectSessionTranscriptTreePathNodes as (\\w+)/)[1]];
const cases=JSON.parse(process.argv[2]);
console.log(JSON.stringify(cases.map(rows=>{const tree=scan(rows);return (tree.hasExplicitLeafUpdate?select(tree,tree.leafId).map(n=>n.entry):rows).map(r=>r.id)})));
"""
        result = subprocess.run(["node", "--input-type=module", "-e", script, str(self.package), json.dumps(cases)],
                                capture_output=True, text=True, check=True)
        for entries, expected in zip(cases, json.loads(result.stdout)):
            rows = [{"meta": entry} for entry in entries]
            self.assertEqual([row["meta"]["id"] for row in select_rows(rows)], expected)


if __name__ == "__main__":
    unittest.main()
