import base64
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock

from agent_manage.response import MAX_PROTOCOL_RESPONSE_BYTES, build_success_response, print_json
from agent_manage.adapters.openclaw.v2026_7.segment_reader import SegmentReader


class MediaResponseBudgetTest(unittest.TestCase):
    def test_five_mib_media_chunk_survives_the_cli_envelope(self):
        reader = object.__new__(SegmentReader)
        reader.input = {"action": "media"}
        reader.ref_token = Mock(return_value="cursor-" + "x" * 4096)
        content = b"x" * (5 * 1024 * 1024 + 1)
        part = reader.chunk(content, 0, {"pointer": "/content/0", "source": "file"},
                            messageId="media", sha256="0" * 64)
        self.assertEqual(len(base64.b64decode(part["data"])), 5 * 1024 * 1024)
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            print_json(build_success_response(part))
        envelope = json.loads(output.getvalue())
        self.assertIsNone(envelope["error"])
        self.assertEqual(base64.b64decode(envelope["result"]["data"]), content[:-1])
        self.assertLessEqual(len(output.getvalue().encode()), MAX_PROTOCOL_RESPONSE_BYTES)
        final = reader.chunk(content, 5 * 1024 * 1024,
                             {"pointer": "/content/0", "source": "file"},
                             messageId="media", sha256="0" * 64)
        self.assertEqual(base64.b64decode(final["data"]), b"x")
        self.assertIsNone(final["nextCursor"])


if __name__ == "__main__":
    unittest.main()
