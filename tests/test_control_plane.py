"""Check capability gates and keep discovery separate from authorization."""
import json
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import control_plane as control


class ControlPlaneTests(unittest.TestCase):
    def test_service_snapshot_reuses_separate_inference_observation(self):
        observed = dict(inference={'state': 'ready', 'detail': 'Independent probe'},
                        harness={'configured': False}, models=[], inference_services=[], inference_sampled_at=123)
        with patch.object(control, 'capture', return_value=None), \
                patch.object(control, 'tailscale_status', return_value=(None, {})), \
                patch.object(control, 'service_rows', return_value=[]), \
                patch.object(control, 'application_rows', return_value=[]), \
                patch.object(control, 'observe_inference') as inference, \
                patch.object(control, 'model_rows') as profiles:
            result = control.snapshot(include_hardware=False, inference_observation=observed)
        inference.assert_not_called()
        profiles.assert_not_called()
        self.assertEqual(result['inference_sampled_at'], 123)

    def test_local_inference_does_not_wait_for_remote_or_harness(self):
        status = dict(state='ready', endpoints={})
        with patch.object(control, 'inference_status', return_value=status), \
                patch.object(control, 'remote_services', side_effect=AssertionError('remote probe')), \
                patch.object(control.harness_service, 'observe', side_effect=AssertionError('harness probe')):
            result = control.observe_inference(control.ROOT, [], include_remote=False)
        self.assertEqual(result['inference'], status)
        self.assertNotIn('harness', result)
        self.assertIn('inference_sampled_at', result)

    def test_published_addresses_exclude_unpublished_and_invalid_ports(self):
        self.assertEqual(control.published_addresses([
            {'URL': '127.0.0.1', 'PublishedPort': 3000, 'Protocol': 'tcp'},
            {'URL': '', 'PublishedPort': 0, 'Protocol': 'tcp'},
            {'URL': 'secret-not-an-address', 'PublishedPort': 3000, 'Protocol': 'tcp'},
            {'URL': '127.0.0.1', 'PublishedPort': 3000, 'Protocol': 'udp'},
            {'URL': '127.0.0.1', 'PublishedPort': True, 'Protocol': 'tcp'}]), ['127.0.0.1:3000'])

    def test_stack_tunnel_is_separate_from_native_peer_networking(self):
        stopped = [{'name': 'tailscale', 'state': 'exited', 'health': '—'}]
        with patch.object(control, 'capture') as probe:
            self.assertEqual(control.stack_tunnel(stopped).state, 'disabled')
            probe.assert_not_called()
        running = [{'name': 'tailscale', 'state': 'running', 'health': 'healthy'}]
        for response, state in [('{}', 'disabled'), ('{"TCP":{"443":{}}}', 'enabled'),
                                (None, 'unknown')]:
            with patch.object(control, 'capture', return_value=response):
                self.assertEqual(control.stack_tunnel(running).state, state)

    def test_deployment_contract_rejects_native_apps_and_wrong_engine_runtime(self):
        config = control.catalog()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'config').mkdir()
            for target, field, invalid in [(config['services'][1], 'runtime', 'native'),
                                           (config['models'][0], 'runtime', 'docker'),
                                           (config['deployment'], 'router_database', True)]:
                original = target[field]
                target[field] = invalid
                (root / 'config/stack.json').write_text(json.dumps(config))
                with self.subTest(field=field, invalid=invalid), self.assertRaises(ValueError):
                    control.catalog(root)
                target[field] = original

    def test_application_state_is_owned_by_services(self):
        observed = [{'name': 'chat', 'state': 'running', 'health': 'healthy'},
                    {'name': 'agent', 'state': 'running', 'health': 'healthy'},
                    {'name': 'caddy', 'state': 'running', 'health': 'unhealthy'}]
        rows = {s['id']: s for s in control.application_rows(observed)}
        self.assertEqual(rows['chat']['state'], 'healthy')
        self.assertEqual(rows['chat']['state_owner'], 'Chat Harness')
        self.assertIn('sessions', rows['chat']['retains'])
        self.assertEqual(rows['agent']['state'], 'healthy')
        self.assertEqual(rows['agent']['state_owner'], 'Agent Harness')
        self.assertNotIn('compose:agent', rows)
        self.assertEqual(rows['caddy']['state'], 'unhealthy')
        self.assertEqual(rows['authelia']['state'], 'unknown')


    def test_discovered_peers_do_not_gain_trust(self):
        peers = {'Peer': {
            'one': {'HostName': 'one', 'OS': 'linux', 'Online': True, 'Active': True,
                    'TailscaleIPs': ['100.64.0.1'], 'CurAddr': '192.0.2.1:41641'},
            'two': {'HostName': 'two', 'OS': 'macOS', 'Online': True, 'Active': True, 'Relay': 'test'},
            'three': {'HostName': 'three', 'Online': True},
            'four': {'HostName': 'four', 'Online': False},
            'five': {'HostName': 'five', 'Online': True, 'Active': True,
                     'PeerRelay': 'test', 'CurAddr': '192.0.2.2:41641'},
        }}
        rows = control.device_rows(peers)
        self.assertTrue(all(row['trust'] == 'Discovered; not enrolled' for row in rows))
        connections = {row['name']: row['connection'] for row in rows}
        self.assertEqual(connections['three'], 'Online; path unknown')
        self.assertEqual(connections['four'], 'Offline')
        self.assertIn('Direct', connections['one'])
        self.assertIn('Relay', connections['two'])
        self.assertIn('Peer relay', connections['five'])

    def test_credential_probe_returns_only_configured_key_names(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.env'
            path.write_text('# TOKEN=private\nWEBUI_SECRET_KEY=private-value\n'
                            'WEBUI_ADMIN_EMAIL=""\nWEBUI_ADMIN_PASSWORD= # set later\n')
            keys = control.configured_keys(path)
            self.assertEqual(keys, {'WEBUI_SECRET_KEY'})
            self.assertNotIn('private-value', json.dumps(sorted(keys)))

    def test_probe_errors_do_not_return_secrets(self):
        self.assertIsNone(control.capture([sys.executable, '-c',
            'import sys; print("secret"); print("secret",file=sys.stderr); sys.exit(1)']))
        self.assertIsNone(control.capture([sys.executable, '-c', 'import time; time.sleep(60)'], timeout=.1))

    def test_probe_timeout_and_completion_stop_owned_descendants(self):
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                     start_new_session=True)
        try:
            for inherited_output in (True, False):
                with self.subTest(inherited_output=inherited_output), tempfile.TemporaryDirectory() as folder:
                    record = Path(folder) / 'child.pid'
                    redirect = '' if inherited_output else ', stdout=subprocess.DEVNULL'
                    script = ('import pathlib,subprocess,sys; '
                              f'child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"]{redirect}); '
                              f'pathlib.Path({str(record)!r}).write_text(str(child.pid)); '
                              'print("ready",flush=True)')
                    result = control.capture([sys.executable, '-c', script], timeout=.5)
                    self.assertEqual(result, None if inherited_output else 'ready\n')
                    child = int(record.read_text())
                    state = subprocess.run(['ps', '-p', str(child), '-o', 'stat='],
                                           capture_output=True, text=True).stdout.strip()
                    self.assertTrue(not state or 'Z' in state, 'Owned probe child is still running')
                    self.assertIsNone(unrelated.poll())
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=2)

    def test_native_tailscale_failure_can_use_existing_container(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / '.env').write_text('')
            with patch.object(control.shutil, 'which', return_value='tool'), \
                    patch.object(control, 'capture', side_effect=[None, '{"BackendState":"Running"}']) as capture:
                source, status = control.tailscale_status(root)
            self.assertEqual(source, 'container')
            self.assertEqual(status['BackendState'], 'Running')
            self.assertIn('exec', capture.call_args.args[0])
            self.assertNotIn('up', capture.call_args.args[0])

    def test_installed_artifacts_do_not_claim_hash_verification(self):
        rows = control.model_rows()
        for row in rows:
            if row['status'] == 'available':
                self.assertIn(row['artifacts'], ('Present; hash not checked', 'Download required'))


    def test_private_network_defaults_and_browser_auth_are_explicit(self):
        config = control.catalog()
        self.assertEqual(config['device_mode'], 'independent_servers')
        self.assertEqual(config['network']['default'], 'private_tailnet')
        self.assertFalse(config['network']['discovery_implies_trust'])
        self.assertFalse(config['network']['public_funnel_by_default'])
        self.assertFalse(config['network']['plaintext_lan_fallback'])
        self.assertEqual(config['identity']['status'], 'available')
        self.assertEqual(config['identity']['methods'], ['password_webauthn'])

    def test_unknown_gateway_model_is_an_observation_failure(self):
        opener = type('Opener', (), {'open': lambda self, *a, **kw: io.BytesIO(
            b'{"gateway":"ready","active_model":{"unexpected":"data"}}')})()
        with patch.object(control.urllib.request, 'build_opener', return_value=opener):
            self.assertEqual(control.gateway_status(), 'Gateway reported an unknown model')

    @patch.object(control, 'configured_models', side_effect=lambda config: config['models'])
    def test_one_gateway_probe_supplies_consistent_state_for_all_models(self, configured):
        from unittest.mock import Mock
        opener = Mock()
        opener.open.side_effect = [io.BytesIO(json.dumps({'gateway': 'ready', 'active_model': model}).encode())
                                   for model in ('qwen-omlx', 'deepseek')]
        with patch.object(control.urllib.request, 'build_opener', return_value=opener):
            result = control.inference_status()
        self.assertEqual(result['state'], 'ready')
        opener.open.assert_called_once()
        self.assertEqual({row['active_model'] for row in result['endpoints'].values()}, {'qwen-omlx'})

    def test_residency_and_activity_are_per_model_and_do_not_mutate_previous_rows(self):
        models = [dict(id=key, status='available') for key in ('qwen', 'deepseek')]
        endpoint = dict(state='ready', active_model='qwen', generating_model='qwen', activity_observed=True)
        status = dict(state='ready', endpoints={key: dict(endpoint, endpoint=f'http://127.0.0.1:{port}/v1')
                                               for key, port in [('qwen', 8000), ('deepseek', 8001)]})
        with patch.object(control, 'inference_status', return_value=status), \
                patch.object(control, 'remote_services', return_value=[]), \
                patch.object(control, 'local_services', return_value=[]):
            observed = control.observe_inference(control.ROOT, models)
        self.assertEqual([(m['runtime'], m['activity']) for m in observed['models']],
                         [('loaded', 'running'), ('standby', 'unloaded')])
        self.assertNotIn('runtime', models[0])

    def test_services_include_stopped_and_removed_containers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.env').write_text('')
            stopped = {'Service': 'webui', 'Name': 'dotagents-webui-1', 'State': 'exited', 'Health': '',
                       'Command': 'secret-value-must-not-be-displayed'}
            running = {'Service': 'caddy', 'Name': 'dotagents-caddy-1', 'State': 'running', 'Health': 'healthy',
                       'Publishers': [{'URL': '127.0.0.1', 'PublishedPort': 3000, 'Protocol': 'tcp'}]}
            for output in (json.dumps([stopped, running]),
                           '\n'.join(map(json.dumps, [stopped, running]))):
                with patch.object(control, 'capture', side_effect=['webui\ncaddy\nidentity\n', output]) as probe:
                    rows = control.service_rows(root)
                by_name = {r['name']: r for r in rows}
                self.assertEqual(by_name['webui']['state'], 'exited')
                self.assertEqual(by_name['identity']['state'], 'not created')
                self.assertEqual(by_name['caddy']['health'], 'healthy')
                self.assertEqual(by_name['caddy']['container_name'], 'dotagents-caddy-1')
                self.assertEqual(by_name['webui']['container_name'], 'dotagents-webui-1')
                self.assertEqual(by_name['caddy']['published'], ['127.0.0.1:3000'])
                self.assertNotIn('secret-value', json.dumps(rows))
                self.assertIn('--all', probe.call_args.args[0])
                self.assertNotIn('up', probe.call_args.args[0])

    def test_failed_service_probe_does_not_report_stopped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.env').write_text('')
            for output in (None, 'broken-json', 'false', '[42]'):
                with patch.object(control, 'capture', side_effect=['webui\n', output]):
                    rows = control.service_rows(root)
                self.assertEqual(rows[0]['state'], 'unknown')
            with patch.object(control, 'capture', side_effect=['webui\n', '[]']):
                self.assertEqual(control.service_rows(root)[0]['state'], 'not created')


if __name__ == '__main__':
    unittest.main()
