import concurrent.futures
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_manage.core.errors import APIError
from agent_manage.core.recovery import recover_refreshes, fingerprint
from agent_manage.core.runtime import RuntimeContext
from agent_manage.core.transactions import runtime_write_lock
from agent_manage.file_io import write_private_json
from agent_manage.local import LocalRunner
from agent_manage.models import AddTelegramBotRequest, RefreshAgentRequest
from agent_manage.orchestrator import InstanceManagerV2
from agent_manage.segment_reader import SegmentReader
from agent_manage.segment_timing import CURRENT, ReadTiming


class BoundaryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_native_processes_are_scoped_without_mutating_global_environment(self):
        contexts = [RuntimeContext(str(n), str(n), self.root / str(n) / "config.json", self.root / str(n)) for n in range(2)]
        def read(context):
            with context.activate():
                result = LocalRunner().run([sys.executable, "-c", "import json,os; print(json.dumps([os.getenv('OPENCLAW_CONFIG_PATH'),os.getenv('OPENCLAW_STATE_DIR'),os.getenv('OPENCLAW_PROFILE')]))"])
                return json.loads(result.stdout)
        with patch.dict(os.environ, {"OPENCLAW_CONFIG_PATH": "wrong", "OPENCLAW_PROFILE": "wrong"}):
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                results = list(pool.map(read, contexts))
            self.assertEqual(os.environ["OPENCLAW_CONFIG_PATH"], "wrong")
        self.assertEqual(results, [[str(c.config_path), str(c.state_dir), None] for c in contexts])

    def test_service_runtime_does_not_inherit_another_environments_credentials(self):
        context = RuntimeContext("one", "env", self.root / "openclaw.json", self.root,
                                 home=self.root, inherit_process_env=False, extra_env=(("SCOPED_API_KEY", "right"),))
        with patch.dict(os.environ, {"OPENAI_API_KEY": "wrong", "OPENCLAW_GATEWAY_TOKEN": "wrong"}), context.activate():
            environment = LocalRunner()._command_env()
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertNotIn("OPENCLAW_GATEWAY_TOKEN", environment)
        self.assertEqual(environment["SCOPED_API_KEY"], "right")
        self.assertEqual(environment["HOME"], str(self.root))

    def test_public_channel_writes_do_not_lose_concurrent_accounts(self):
        config = self.root / "openclaw.json"
        config.write_text(json.dumps({"agents": {"list": [{"id": "demo"}]}}))
        def add(number):
            manager = InstanceManagerV2(LocalRunner(), config_path=str(config))
            manager.runtime_context = RuntimeContext("one", "env", config, self.root)
            original = manager._load_config
            def slow_read():
                data = original()
                time.sleep(.02)
                return data
            manager._load_config = slow_read
            return manager.add_tg_bot(AddTelegramBotRequest("demo", "fixture", str(number)))
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            results = list(pool.map(add, range(4)))
        self.assertTrue(all(result["ok"] for result in results))
        self.assertEqual(set(json.loads(config.read_text())["channels"]["telegram"]["accounts"]), {"0", "1", "2", "3"})

    def test_lock_excludes_another_process_and_is_reentrant(self):
        code = "from agent_manage.core.transactions import runtime_write_lock; import sys;\nwith runtime_write_lock(sys.argv[1],timeout=0): print('acquired')"
        with runtime_write_lock(self.root), runtime_write_lock(self.root):
            result = subprocess.run([sys.executable, "-c", code, str(self.root)], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"lock is busy", result.stderr)
        result = subprocess.run([sys.executable, "-c", code, str(self.root)], capture_output=True)
        self.assertEqual(result.returncode, 0)

    def test_config_compare_and_swap_and_owned_rollback(self):
        config = self.root / "openclaw.json"
        config.write_text('{"a": 1}')
        manager = InstanceManagerV2(LocalRunner(), config_path=str(config))
        snapshot = manager._snapshot_config_file()
        data = manager._load_config()
        config.write_text('{"external": true}')
        with self.assertRaisesRegex(RuntimeError, "changed"):
            manager._write_config(data, "test")
        data = manager._load_config()
        data["ours"] = True
        manager._write_config(data, "test")
        config.write_text('{"external": "later"}')
        self.assertIn("error", manager._safe_restore_config_snapshot(snapshot))
        self.assertEqual(json.loads(config.read_text()), {"external": "later"})

    def journal(self):
        config = self.root / "openclaw.json"
        config.write_text("{}")
        target = self.root / "app.js"
        target.write_text("old")
        before = fingerprint(target)
        backup = self.root / "agent-manage/backups/job"
        backup.mkdir(parents=True)
        (backup / "file-0").write_bytes(target.read_bytes())
        target.write_text("new")
        manifest = {"config_path": str(config), "status": "prepared", "recovery_entries": [
            {"path": str(target), "before": before, "after": fingerprint(target), "backup_file": "file-0"}]}
        write_private_json(backup / "manifest.json", manifest)
        return config, target, backup

    def test_interrupted_refresh_recovers_once(self):
        config, target, backup = self.journal()
        with runtime_write_lock(self.root):
            self.assertEqual(recover_refreshes(config), ["job"])
            self.assertEqual(recover_refreshes(config), [])
        self.assertEqual(target.read_text(), "old")
        self.assertEqual(json.loads((backup / "manifest.json").read_text())["status"], "rolled_back")

    def test_interrupted_refresh_does_not_overwrite_independent_edits(self):
        config, target, _ = self.journal()
        target.write_text("user change")
        with runtime_write_lock(self.root), self.assertRaises(APIError) as raised:
            recover_refreshes(config)
        self.assertEqual(raised.exception.code, "RECOVERY_REQUIRED")
        self.assertEqual(target.read_text(), "user change")

    def test_media_range_does_not_allocate_or_read_whole_file_again(self):
        media = self.root / "image.png"
        media.write_bytes(b"x" * (12 * 1024 * 1024))
        request = {"root": str(self.root), "sessionKey": "agent:main:main", "sessionId": "test",
                   "config": {"agents": {"defaults": {"workspace": str(self.root)}}}}
        reader = SegmentReader(request, os.environ)
        expected = reader.local_media_read(str(media))["sha256"]
        timing = ReadTiming()
        token = CURRENT.set(timing)
        try:
            other = SegmentReader(request, os.environ)
            part = other.local_media_read(str(media), with_bytes=True, offset=1024, length=4096)
        finally:
            CURRENT.reset(token)
        self.assertEqual(part["sha256"], expected)
        self.assertEqual(part["bytes"], b"x" * 4096)
        self.assertEqual(timing.counts["mediaBytesRead"], 4096)
        self.assertEqual(part["totalBytes"], 12 * 1024 * 1024)
        media.write_bytes(b"changed")
        self.assertNotEqual(other.local_media_read(str(media))["sha256"], expected)

    def test_real_refresh_interruption_has_recoverable_write_ahead_journal(self):
        # Use the existing refresh fixture, without modifying any legacy test.
        from test_refresh_management import RefreshManagementTest
        fixture = RefreshManagementTest()
        fixture.setUp()
        try:
            fixture.manager.refresh_agent(RefreshAgentRequest("demo", template_dir=str(fixture.source), template_only=True))
            fixture.write_source("SOUL.md", "version two\n")
            from agent_manage.file_io import atomic_write_bytes
            def interrupted(path, data, mode=0o600):
                atomic_write_bytes(path, data, mode)
                if path == fixture.workspace / "SOUL.md":
                    raise SystemExit("simulated power loss")
            with patch("agent_manage.refresh_management._atomic_bytes", interrupted), self.assertRaises(SystemExit):
                fixture.manager.refresh_agent(RefreshAgentRequest("demo", template_dir=str(fixture.source), template_only=True))
            self.assertEqual((fixture.workspace / "SOUL.md").read_text(), "version two\n")
            with runtime_write_lock(fixture.config_path.parent):
                self.assertEqual(len(recover_refreshes(fixture.config_path)), 1)
            self.assertEqual((fixture.workspace / "SOUL.md").read_text(), "version one\n")
        finally:
            fixture.doCleanups()
