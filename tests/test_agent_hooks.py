import fcntl
import io
import json
import os
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from agent_manage import agent_hooks
from agent_manage.agent_hooks import HookError, run_hook, start_agents
from agent_manage.cli import main
from agent_manage.local import LocalRunner
from agent_manage.orchestrator import InstanceManagerV2
from agent_manage.models import AddAgentsRequest, AddAgentRequest
from agent_manage.response import build_error_response


class AgentHooksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.config = self.root / 'openclaw.json'
        self.config.write_text(json.dumps({'agents': {'list': [{'id': 'demo', 'workspace': str(self.workspace)}]},
            'models': {'providers': {'demo': {'apiKey': 'model-secret-test'}}}}))
        self.state = self.root / 'agent-manage/hooks/demo.json'

    def script(self, name, content):
        (self.workspace / f'{name}.sh').write_text(content)

    def test_missing_and_dry_run_never_write(self):
        self.assertEqual(run_hook(self.config, 'demo', self.workspace, 'install')['reason'], 'script_missing')
        self.script('install', 'touch marker')
        self.assertEqual(run_hook(self.config, 'demo', self.workspace, 'install', dry_run=True)['reason'], 'dry_run')
        self.assertFalse((self.root / 'agent-manage').exists())
        self.assertFalse((self.workspace / 'marker').exists())

    def test_install_runs_once_with_context_then_explicit_retry(self):
        self.script('install', 'printf "%s:%s" "$AGENT_ID" "$OPENCLAW_CONFIG_PATH"; echo run >> count')
        result = run_hook(self.config, 'demo', self.workspace, 'install')
        self.assertEqual(result['stdout'], f'demo:{self.config}')
        self.assertEqual(run_hook(self.config, 'demo', self.workspace, 'install')['reason'], 'already_installed')
        run_hook(self.config, 'demo', self.workspace, 'install', force=True)
        self.assertEqual((self.workspace / 'count').read_text(), 'run\nrun\n')
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)

    def test_failure_is_returned_redacted_and_retry_is_possible(self):
        self.script('install', 'echo model-secret-test; echo "password=generated-secret" >&2; exit 9')
        with self.assertRaises(HookError) as caught:
            run_hook(self.config, 'demo', self.workspace, 'install')
        response = build_error_response(caught.exception)
        self.assertEqual(response['error']['code'], 'AGENT_INSTALL_FAILED')
        details = response['error']['details']
        self.assertEqual(details['returncode'], 9)
        self.assertTrue(details['workspace_preserved'])
        self.assertFalse(details['rollback_performed'])
        text = json.dumps(response) + self.state.read_text()
        self.assertNotIn('model-secret-test', text)
        self.assertNotIn('generated-secret', text)
        self.script('start', 'touch started')
        self.assertEqual(start_agents(self.config)['agents'][0]['reason'], 'installation_incomplete')
        self.script('install', 'echo fixed')
        run_hook(self.config, 'demo', self.workspace, 'install')
        start_agents(self.config)
        self.assertTrue((self.workspace / 'started').exists())

    def test_timeout_cancels_descendants_and_limits_output(self):
        self.script('install', 'printf "%020000d" 0; (sleep 1; touch late) & wait')
        with self.assertRaises(HookError) as caught:
            run_hook(self.config, 'demo', self.workspace, 'install', timeout=0.15)
        result = caught.exception.result
        self.assertTrue(result.timed_out)
        self.assertEqual(result.returncode, 124)
        self.assertLess(len(result.stdout), 17000)
        time.sleep(1.1)
        self.assertFalse((self.workspace / 'late').exists())

    def test_detached_child_does_not_hold_command_response_open(self):
        self.script('start', 'sleep 0.4 & echo launched')
        started = time.monotonic()
        result = run_hook(self.config, 'demo', self.workspace, 'start', timeout=2)
        self.assertLess(time.monotonic() - started, 0.35)
        self.assertIn('launched', result['stdout'])

    def test_lock_prevents_concurrent_install_or_start(self):
        self.script('install', 'touch marker')
        self.state.parent.mkdir(parents=True)
        with self.state.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(HookError):
                run_hook(self.config, 'demo', self.workspace, 'install')
        self.assertFalse((self.workspace / 'marker').exists())

    def test_symlink_hook_is_rejected(self):
        external = self.root / 'external.sh'
        external.write_text('touch marker')
        (self.workspace / 'install.sh').symlink_to(external)
        with self.assertRaises(HookError):
            run_hook(self.config, 'demo', self.workspace, 'install')
        self.assertFalse((self.workspace / 'marker').exists())

    def test_start_failure_does_not_block_next_agent(self):
        self.script('start', 'echo failed >&2; exit 4')
        other = self.root / 'other'; other.mkdir()
        (other / 'start.sh').write_text('touch started')
        config = json.loads(self.config.read_text())
        config['agents']['list'].append({'id': 'other', 'workspace': str(other)})
        self.config.write_text(json.dumps(config))
        result = start_agents(self.config)
        self.assertFalse(result['ok'])
        self.assertEqual(result['agents'][0]['returncode'], 4)
        self.assertTrue((other / 'started').exists())

    def test_cancel_stops_running_hook(self):
        self.script('start', 'sleep 30')
        cancel = threading.Event()
        timer = threading.Timer(0.1, cancel.set); timer.start()
        self.addCleanup(timer.join)
        with self.assertRaises(HookError) as caught:
            run_hook(self.config, 'demo', self.workspace, 'start', cancel=cancel)
        self.assertEqual(caught.exception.result.returncode, 130)

    def test_cli_failure_output(self):
        self.script('install', 'echo dependency-failed >&2; exit 7')
        out = io.StringIO()
        with patch.dict(os.environ, {'UNITAG_AGENT_MANAGER_RUNTIME': ''}), redirect_stdout(out):
            code = main(['--config-path', str(self.config), 'agent-install', '--agent', 'demo'])
        self.assertNotEqual(code, 0)
        response = json.loads(out.getvalue())
        self.assertEqual(response['error']['details']['returncode'], 7)
        self.assertIn('dependency-failed', response['error']['details']['stderr'])

    def test_batch_hooks_wait_until_all_workspace_setup_and_failure_keeps_files(self):
        from test_create_instance_v2 import FakeRunner
        manager = InstanceManagerV2(FakeRunner(), config_path=str(self.config), template_root=str(self.root / 'templates'))
        self.script('install', 'test -f ready || exit 11; echo installed > marker; exit 8')
        spec = AddAgentsRequest(agents=[AddAgentRequest(agent_name='demo', template_name='demo')], workspace_root=str(self.root))
        with patch.object(manager, 'resolve_add_agent_workspace', return_value=self.workspace), \
             patch.object(manager, '_provision_agent_from_template', return_value={'created_agent': True, 'created_workspace': True}), \
             patch.object(manager, '_configure_config_tools', return_value={}), \
             patch.object(manager, '_configure_workspace_defaults', side_effect=lambda *a, **k: (self.workspace / 'ready').touch()), \
             patch.object(manager, '_safe_purge_workspace') as purge, \
             patch.object(manager, '_safe_delete_agent') as delete:
            with self.assertRaises(HookError) as caught:
                manager.add_agents(spec)
        self.assertEqual(caught.exception.result.returncode, 8)
        purge.assert_not_called(); delete.assert_not_called()
        self.assertTrue((self.workspace / 'marker').exists())

    def test_build_defers_hooks(self):
        manager = InstanceManagerV2(LocalRunner(), config_path=str(self.config))
        self.script('install', 'touch marker')
        with patch.dict(os.environ, {'UNITAG_AGENT_HOOKS_DEFER': '1'}):
            self.assertEqual(manager._install_agent_hooks([], ['demo'], [self.workspace]), [])
        self.assertFalse((self.workspace / 'marker').exists())

    def test_cli_start_reports_failure_as_nonzero(self):
        self.script('start', 'echo start-failed >&2; exit 6')
        out = io.StringIO()
        with patch.dict(os.environ, {'UNITAG_AGENT_MANAGER_RUNTIME': ''}), redirect_stdout(out):
            code = main(['--config-path', str(self.config), 'agents-start'])
        self.assertEqual(code, 1)
        response = json.loads(out.getvalue())
        self.assertEqual(response['error']['details']['agents'][0]['returncode'], 6)

    def test_full_batch_is_ready_before_first_hook(self):
        from test_create_instance_v2 import FakeRunner
        manager = InstanceManagerV2(FakeRunner(), config_path=str(self.config), template_root=str(self.root / 'templates'))
        other = self.root / 'other'
        def provision(**kw):
            workspace = kw['workspace']; workspace.mkdir(exist_ok=True)
            (workspace / 'install.sh').write_text('test -f "../ready"; echo ok')
            return {'created_agent': False, 'created_workspace': False}
        def defaults(*args, **kw):
            self.assertTrue((other / 'install.sh').exists())
            (self.root / 'ready').touch()
        spec = AddAgentsRequest(agents=[AddAgentRequest(agent_name=n, template_name=n) for n in ('demo', 'other')], workspace_root=str(self.root))
        with patch.object(manager, 'resolve_add_agent_workspace', side_effect=lambda spec, root: self.workspace if spec.agent_name == 'demo' else other), \
             patch.object(manager, '_provision_agent_from_template', side_effect=provision), \
             patch.object(manager, '_configure_config_tools', return_value={}), \
             patch.object(manager, '_configure_workspace_defaults', side_effect=defaults):
            result = manager.add_agents(spec)
        self.assertEqual(len(result['post_batch_actions']), 2)
        self.assertTrue(all(item['returncode'] == 0 for item in result['post_batch_actions']))
