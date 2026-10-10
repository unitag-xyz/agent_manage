import errno
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

from agent_manage.activation import FlyActivationMixin


CHILD = r'''
import json, os, sys, time
from pathlib import Path
from unittest.mock import patch
from agent_manage.local import LocalRunner
from agent_manage.models import CreateInstanceRequest
from agent_manage.orchestrator import InstanceManagerV2

config, ready, release, hold = sys.argv[1:]
os.environ.update(UNITAG_AGENT_MANAGER_RUNTIME="container",
                  UNITAG_AGENT_MANAGER_ACTIVATION_MODE="fly", OPENCLAW_GATEWAY_TOKEN="")
runner = LocalRunner()
runner.log = lambda *_args: None
manager = InstanceManagerV2(runner, config_path=config)
def catalog(*_args):
    if hold == "1":
        Path(ready).touch()
        while not Path(release).exists():
            time.sleep(0.01)
    return {"models": [{"id": "fixture-model", "model_ref": "unipay-fun/fixture-model",
                        "definition": {"id": "fixture-model", "name": "Fixture"}}]}
try:
    with patch.object(InstanceManagerV2, "_fetch_supported_gateway_models", catalog):
        result = manager.activate_instance(CreateInstanceRequest(model_key="offline-lock-fixture-key"))
    print(json.dumps({"activationRequired": result["activationRequired"],
                      "hash": result["requestedConfigSha256"]}), flush=True)
except Exception as error:
    print(json.dumps({"busy": "lock is busy" in str(error)}), flush=True)
    sys.exit(1)
'''


class ActivationLockTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.lock = self.root / '.openclaw.json.activation.lock'

    def test_unlocked_existing_file_is_reused_without_truncation_or_unlink(self):
        self.lock.write_bytes(b'old unlocked sentinel')
        self.lock.chmod(0o600)
        original = self.lock.stat()
        with FlyActivationMixin._activation_lock(self.lock):
            self.assertEqual(self.lock.stat().st_ino, original.st_ino)
        self.assertEqual(self.lock.read_bytes(), b'old unlocked sentinel')
        self.assertEqual(self.lock.stat().st_ino, original.st_ino)
        with FlyActivationMixin._activation_lock(self.lock):
            pass

    def test_busy_fails_without_waiting_or_deleting_holder_file(self):
        with FlyActivationMixin._activation_lock(self.lock):
            original = self.lock.stat()
            start = time.monotonic()
            with self.assertRaisesRegex(RuntimeError, 'lock is busy'):
                with FlyActivationMixin._activation_lock(self.lock):
                    self.fail('Concurrent lock acquired')
            self.assertLess(time.monotonic() - start, 2)
            self.assertEqual(self.lock.stat().st_ino, original.st_ino)
        with FlyActivationMixin._activation_lock(self.lock):
            pass

    def test_exception_releases_lock_and_retains_inode(self):
        with self.assertRaisesRegex(ValueError, 'synthetic failure'):
            with FlyActivationMixin._activation_lock(self.lock):
                raise ValueError('synthetic failure')
        with FlyActivationMixin._activation_lock(self.lock):
            pass

    @unittest.skipUnless(os.name == 'posix', 'POSIX descriptor security checks')
    def test_owned_private_regular_noninherited_descriptor(self):
        real_flock = __import__('fcntl').flock
        def checked(descriptor, operation):
            self.assertFalse(os.get_inheritable(descriptor))
            self.assertEqual(os.fstat(descriptor).st_mode & 0o777, 0o600)
            return real_flock(descriptor, operation)
        with patch('fcntl.flock', side_effect=checked):
            with FlyActivationMixin._activation_lock(self.lock):
                pass

    @unittest.skipUnless(os.name == 'posix', 'POSIX owner checks')
    def test_wrong_owner_fails_before_flock_without_changing_file(self):
        self.lock.write_bytes(b'keep owner file')
        self.lock.chmod(0o600)
        with patch('agent_manage.activation.os.geteuid', return_value=os.geteuid() + 1), \
                patch('fcntl.flock') as flock:
            with self.assertRaisesRegex(RuntimeError, 'Unsafe activation lock'):
                with FlyActivationMixin._activation_lock(self.lock):
                    self.fail('Unsafe lock acquired')
            flock.assert_not_called()
        self.assertEqual(self.lock.read_bytes(), b'keep owner file')

    @unittest.skipUnless(os.name == 'posix', 'POSIX symlink/FIFO/hardlink checks')
    def test_symlink_fifo_hardlink_directory_and_permissions_fail_closed(self):
        target = self.root / 'target'
        target.write_bytes(b'untouched')
        target.chmod(0o600)
        for kind in ('symlink', 'fifo', 'hardlink', 'directory', 'permissions'):
            with self.subTest(kind=kind):
                if kind == 'symlink':
                    self.lock.symlink_to(target)
                elif kind == 'fifo':
                    os.mkfifo(self.lock, 0o600)
                elif kind == 'hardlink':
                    os.link(target, self.lock)
                elif kind == 'directory':
                    self.lock.mkdir()
                else:
                    self.lock.write_bytes(b'keep unsafe file')
                    self.lock.chmod(0o644)
                try:
                    with self.assertRaises((RuntimeError, OSError)):
                        with FlyActivationMixin._activation_lock(self.lock):
                            self.fail('Unsafe lock acquired')
                    self.assertEqual(target.read_bytes(), b'untouched')
                    self.assertTrue(self.lock.exists())
                finally:
                    if kind == 'directory':
                        self.lock.rmdir()
                    else:
                        self.lock.unlink()

    @unittest.skipUnless(os.name == 'posix', 'POSIX inode replacement checks')
    def test_path_replaced_during_acquire_is_not_deleted_or_used(self):
        real_flock = __import__('fcntl').flock
        def replace_path(descriptor, operation):
            real_flock(descriptor, operation)
            replacement = self.root / 'replacement'
            replacement.write_bytes(b'other inode')
            replacement.chmod(0o600)
            os.replace(replacement, self.lock)
        with patch('fcntl.flock', side_effect=replace_path):
            with self.assertRaisesRegex(RuntimeError, 'Unsafe activation lock'):
                with FlyActivationMixin._activation_lock(self.lock):
                    self.fail('Replaced lock acquired')
        self.assertEqual(self.lock.read_bytes(), b'other inode')
        with FlyActivationMixin._activation_lock(self.lock):
            pass

    @unittest.skipUnless(os.name == 'posix', 'POSIX unexpected flock error')
    def test_non_busy_os_error_propagates_and_closes_descriptor(self):
        with patch('fcntl.flock', side_effect=OSError(errno.EIO, 'synthetic IO')):
            with self.assertRaises(OSError):
                with FlyActivationMixin._activation_lock(self.lock):
                    self.fail('Broken lock acquired')
        with FlyActivationMixin._activation_lock(self.lock):
            pass


@unittest.skipUnless(os.name == 'posix', 'POSIX kill/flock regression requires Linux image')
class ActivationProcessLockTests(unittest.TestCase):
    def setUp(self):
        ActivationLockTests.setUp(self)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.config = self.root / 'openclaw.json'
        self.original = json.dumps({'agents': {'list': [{'id': 'team', 'workspace': str(self.workspace)}]},
                                    'gateway': {'auth': {'mode': 'token', 'token': 'offline-gateway'}}}).encode()
        self.config.write_bytes(self.original)
        self.ready = self.root / 'ready'
        self.release = self.root / 'release'

    def spawn(self, hold):
        child = subprocess.Popen([sys.executable, '-c', CHILD, str(self.config),
                                  str(self.ready), str(self.release), '1' if hold else '0'],
                                 cwd=Path(__file__).resolve().parents[1],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def cleanup():
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)
        self.addCleanup(cleanup)
        if hold:
            deadline = time.monotonic() + 10
            while not self.ready.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(self.ready.exists(), 'Child failed to reach held activation transaction')
        return child

    def assert_retry_commits(self, inode):
        child = self.spawn(False)
        output, _ = child.communicate(timeout=15)
        self.assertEqual(child.returncode, 0)
        result = json.loads(output)
        self.assertTrue(result['activationRequired'])
        self.assertEqual(result['hash'], hashlib.sha256(self.config.read_bytes()).hexdigest())
        self.assertEqual(self.lock.stat().st_ino, inode)

    def test_sigkill_mid_transaction_releases_kernel_lock_and_retry_commits(self):
        child = self.spawn(True)
        inode = self.lock.stat().st_ino
        child.kill()
        child.communicate(timeout=5)
        self.assertEqual(child.returncode, -9)
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assert_retry_commits(inode)

    def test_executor_style_timeout_kill_does_not_block_retry(self):
        child = self.spawn(True)
        inode = self.lock.stat().st_ino
        with self.assertRaises(subprocess.TimeoutExpired):
            child.communicate(timeout=0.1)
        child.kill()
        child.communicate(timeout=5)
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assert_retry_commits(inode)

    def test_competing_process_is_busy_then_retries_after_holder_completes(self):
        holder = self.spawn(True)
        inode = self.lock.stat().st_ino
        competitor = self.spawn(False)
        output, _ = competitor.communicate(timeout=10)
        self.assertEqual(competitor.returncode, 1)
        self.assertTrue(json.loads(output)['busy'])
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertEqual(self.lock.stat().st_ino, inode)
        self.release.touch()
        holder.communicate(timeout=15)
        self.assertEqual(holder.returncode, 0)
        self.assert_retry_commits(inode)


if __name__ == '__main__':
    unittest.main()
