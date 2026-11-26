"""Opt-in offline smoke in a disposable OpenClaw container, never on a live instance."""
import io
import json
import os
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from agent_manage.cli import main
from agent_manage.orchestrator import InstanceManagerV2


@unittest.skipUnless(os.environ.get("AM_DISPOSABLE_CLI_SMOKE") == "1", "requires an empty disposable tmpfs")
class NativeCliSmoke(unittest.TestCase):
    def test_existing_invocations_create_attach_and_refresh(self):
        root = Path("/home/node/.openclaw")
        self.assertEqual(os.environ.get("UNITAG_AGENT_MANAGER_RUNTIME"), "container")
        self.assertTrue(any(line.split()[1:3] == [str(root), "tmpfs"] for line in Path("/proc/mounts").read_text().splitlines()))
        self.assertEqual(list(root.iterdir()), [])
        (root / "templates").mkdir()
        for name in ("routing-alpha", "routing-beta"):
            with zipfile.ZipFile(root / "templates" / (name + ".zip"), "w") as archive:
                archive.writestr("template.yaml", "version: 1.0.0\n")
                archive.writestr("SOUL.md", name)

        def run(argv):
            stream = io.StringIO()
            with redirect_stdout(stream), patch("sys.stdin", io.StringIO("fixture-only\n")):
                code = main(argv)
            reply = json.loads(stream.getvalue())
            self.assertEqual(code, 0, (argv[0], reply.get("error"), reply["message"]))
            self.assertIsNone(reply["error"])

        # OpenClaw provisioning historically starts with an initialized config,
        # not a running Gateway. Engine selection does not create that config.
        config = root / "openclaw.json"
        config.write_text('{"gateway":{"mode":"local"},"agents":{"list":[{"id":"main"}]}}')
        catalog = {"models": [{"id": "chat", "model_ref": "fixture/chat", "definition": {"id": "chat", "name": "Chat"}}]}
        with patch.object(InstanceManagerV2, "_fetch_supported_gateway_models", return_value=catalog):
            run(["create-instance", "--template-name", "routing-alpha", "--model-key-stdin"])
            run(["add-agent", "--template-name", "routing-beta"])
            source = root / "templates" / "routing-alpha"
            (source / "template.yaml").write_text("version: 1.1.0\n")
            (source / "SOUL.md").write_text("refreshed alpha")
            (source / "release-note.txt").write_text("1.1.0")
            refresh = ["refresh-agent", "--agent", "routing-alpha", "--template-dir", str(source), "--template-only"]
            run(refresh)
            alpha = root / "data" / "routing-alpha"
            self.assertEqual((alpha / "release-note.txt").read_text(), "1.1.0")
            self.assertEqual((alpha / "SOUL.md").read_text(), "routing-alpha")
            run([*refresh, "--replace-modified"])
        agents = {a["id"]: a for a in json.loads(config.read_text())["agents"]["list"]}
        for agent, expected in (("routing-alpha", "refreshed alpha"), ("routing-beta", "routing-beta")):
            self.assertEqual((Path(agents[agent]["workspace"]) / "SOUL.md").read_text(), expected)
