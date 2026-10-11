"""Check Harness workspace and HTTP boundaries without live application calls."""
from pathlib import Path
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import harness_service


class HarnessTests(unittest.TestCase):
    def test_local_routes_share_the_configured_port_and_keep_model_provider_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = Path(__file__).resolve().parents[1]
            (root / 'config').mkdir()
            (root / '.env').touch()
            config = dict(models=dict(default='qwen-omlx', enabled=['qwen-omlx', 'deepseek']),
                          network=dict(gateway_port=9876),
                          applications=dict(images=False, remote_browser=False))
            (root / 'config.yaml').write_text(json.dumps(config))
            for name in ('models.json', 'harness-policy.json'):
                shutil.copyfile(source / 'config' / name, root / 'config' / name)
            with patch.object(harness_service, 'load_endpoints', return_value=[]), \
                    patch.object(harness_service, 'chat_endpoints', return_value=[]):
                harness_service.write_endpoints(root)
                routes = json.loads((root / '.local/harness/endpoints.json').read_text())
                rows = harness_service.profile(root, 'chat')
            providers = next(row['config']['providers'] for row in rows if row['id'] == 'llm-pi-ai')
            self.assertEqual({route['provider'] for route in routes}, {'dotagents-qwen-omlx', 'dotagents-deepseek'})
            for route in routes:
                self.assertEqual(route['baseURL'], 'http://host.docker.internal:9876/v1')
                provider = providers[route['provider']]
                self.assertEqual(provider['baseURL'], route['baseURL'])
                self.assertEqual(provider['models'][0]['id'], route['model'])

    def test_offline_remote_node_does_not_block_independent_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'config').mkdir()
            (root / '.env').touch()
            source = Path(__file__).resolve().parents[1]
            shutil.copyfile(source / 'config.yaml', root / 'config.yaml')
            for name in ('models.json', 'harness-policy.json'):
                shutil.copyfile(source / 'config' / name, root / 'config' / name)
            with patch.object(harness_service, 'chat_endpoints', side_effect=OSError):
                for role in ('chat', 'agent'):
                    rows = harness_service.profile(root, role)
                    presets = [p['id'] for row in rows for p in row.get('insert', [])
                               if p['id'].startswith('preset-')]
                    self.assertEqual(presets, ['preset-' + role])

    def test_chat_and_agent_mount_only_their_own_data_and_workspace(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(['docker', 'compose', '--env-file', '/dev/null',
                                 '-f', str(root / 'config/compose.harness.yaml'),
                                 'config', '--format', 'json'], capture_output=True, text=True, check=True)
        services = json.loads(result.stdout)['services']
        self.assertNotIn('harness', services)
        for role in ('chat', 'agent'):
            service = services[role]
            self.assertEqual(service['environment']['HARNESS_ROLE'], role)
            self.assertEqual('DOTAGENTS_THINGS_TOKEN' in service['environment'], role == 'chat')
            self.assertEqual('DOTAGENTS_CALENDAR_TOKEN' in service['environment'], role == 'chat')
            self.assertEqual('DOTAGENTS_CALENDAR_ENABLED' in service['environment'], role == 'chat')
            self.assertEqual([(p['host_ip'], p['published'], p['target']) for p in service['ports']],
                             [('127.0.0.1', '3002' if role == 'chat' else '3003', 3081)])
            self.assertEqual({v['target']: v['source'] for v in service['volumes'] if not v.get('read_only')},
                             {'/data': str(root / 'sessions' / role),
                              '/workspace': str(root / 'sessions' / role / 'workspace')})
            self.assertTrue(service['read_only'])

    def test_context_and_tool_output_policy(self):
        if not shutil.which('node'):
            self.fail('Node.js is required for the Harness JavaScript tests')
        subprocess.run(['node', '--test', str(Path(__file__).with_name('harness_policy.test.mjs'))], check=True)

    def test_live_model_catalog(self):
        if not shutil.which('node'):
            self.fail('Node.js is required for the Harness JavaScript tests')
        subprocess.run(['node', '--test', str(Path(__file__).with_name('harness_models.test.mjs'))], check=True)

    def test_session_model_lock(self):
        if not shutil.which('node'):
            self.fail('Node.js is required for the Harness JavaScript tests')
        subprocess.run(['node', '--test', str(Path(__file__).with_name('harness_session_model.test.mjs'))], check=True)
        subprocess.run(['node', '--test', str(Path(__file__).with_name('harness_things_schema.test.mjs'))], check=True)

    def test_workspace_rejects_other_paths_and_symlink_redirects(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            with patch.object(Path, 'home', return_value=home):
                allowed = harness_service.workspace(root=home)
                self.assertEqual(allowed, home / 'sessions/agent/workspace')
                self.assertNotEqual(harness_service.workspace(root=home, role='chat'), allowed)
                with self.assertRaises(ValueError):
                    harness_service.workspace(home, root=home)
                allowed.rmdir()
                allowed.symlink_to(home, target_is_directory=True)
                with self.assertRaises(ValueError):
                    harness_service.workspace(root=home)

    def test_application_readiness_does_not_require_all_models_online(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.local/harness').mkdir(parents=True)
            (root / '.local/harness/profile.json').write_text('[]')
            with patch.object(harness_service, 'request', return_value={'ready': True, 'active': 1}):
                result = harness_service.observe(root)
            self.assertTrue(result['session_active'])
            self.assertTrue(result['activity']['coordinator'])
            with patch.object(harness_service, 'request', side_effect=OSError):
                self.assertFalse(harness_service.observe(root)['session_active'])

    def test_chat_browser_does_not_expose_agent_execution_tools(self):
        if not shutil.which('node'):
            self.fail('Node.js is required for the Harness JavaScript tests')
        subprocess.run(['node', '--test', str(Path(__file__).with_name('chat_browser.test.mjs'))], check=True)

    def test_bridge_authentication_and_session_ownership(self):
        if not shutil.which('node'):
            self.fail('Node.js is required for the Harness JavaScript tests')
        subprocess.run(['node', '--test', str(Path(__file__).with_name('harness_bridge.test.mjs'))], check=True)


if __name__ == '__main__':
    unittest.main()
