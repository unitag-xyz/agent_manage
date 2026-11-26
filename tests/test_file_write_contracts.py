import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_manage.adapters.openclaw.manager_core import ManagerCore
from agent_manage.adapters.openclaw.refresh_management import _atomic_bytes
from agent_manage.adapters.openclaw.seed import _atomic_write_runtime_config


class FileWriteContractsTest(unittest.TestCase):
    def test_writers_preserve_original_and_remove_staging_on_replace_failure(self):
        for writer in ("config", "refresh", "seed"):
            with self.subTest(writer=writer), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.json"
                path.write_bytes(b'{"original":true}\n')
                before = path.read_bytes()
                with patch("os.replace", side_effect=OSError("replace failed")):
                    with self.assertRaises(OSError):
                        self._write(writer, path)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(sorted(p.name for p in path.parent.iterdir()),
                                 ["config.json", "config.json.bak"] if writer == "config" else ["config.json"])

    def test_writers_publish_private_files_and_only_config_writer_keeps_backup(self):
        for writer in ("config", "refresh", "seed"):
            with self.subTest(writer=writer), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.json"
                before = b'{"original":true}\n'
                path.write_bytes(before)
                self._write(writer, path)
                self.assertEqual(json.loads(path.read_bytes()), {"new": True})
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                backup = path.with_suffix(".json.bak")
                self.assertEqual(backup.exists(), writer == "config")
                if backup.exists():
                    self.assertEqual(backup.read_bytes(), before)

    @staticmethod
    def _write(writer, path):
        if writer == "config":
            ManagerCore(Mock(dry_run=False, openclaw_bin="openclaw"), config_path=str(path))._write_config(
                {"new": True}, note="test")
        elif writer == "refresh":
            _atomic_bytes(path, b'{"new":true}\n')
        else:
            _atomic_write_runtime_config(path, '{"new":true}\n')
