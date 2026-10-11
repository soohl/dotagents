"""Gateway discovery stays on online Tailscale peers and tolerates absent nodes."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import node_discovery as discovery
from inference_observations import load_endpoints


class DiscoveryTests(unittest.TestCase):
    def test_only_online_tailnet_addresses_are_probed(self):
        status = dict(Self=dict(TailscaleIPs=['100.64.0.1']), Peer={
            'self': dict(Online=True, TailscaleIPs=['100.64.0.1']),
            'offline': dict(Online=False, TailscaleIPs=['100.64.0.2']),
            'public': dict(Online=True, TailscaleIPs=['192.0.2.2']),
            'loopback': dict(Online=True, TailscaleIPs=['127.0.0.1']),
            'online': dict(Online=True, HostName='example-node', TailscaleIPs=['100.64.0.3']),
            'duplicate': dict(Online=True, TailscaleIPs=['100.64.0.3']),
            'ipv6': dict(Online=True, TailscaleIPs=['fd7a:115c:a1e0::2']),
        })
        peers = list(discovery.candidates(status, 9999))
        self.assertEqual([p['base_url'] for p in peers],
                         ['http://100.64.0.3:9999/v1', 'http://[fd7a:115c:a1e0::2]:9999/v1'])

    def test_invalid_closed_and_unknown_gateways_are_ignored(self):
        endpoint = dict(id='test', device='test', base_url='http://100.64.0.2:8888/v1',
                        approved=True, access='network')
        for response in ({}, {'data': 'wrong'}, {'data': [{'id': 'bad\nlabel'}]},
                         {'data': [{}]}, {'data': [], 'dotagents':
                         {'protocol': 'dotagents-node', 'schema_version': 2}}):
            with self.subTest(response=response), patch.object(discovery, 'read_json', return_value=response):
                self.assertIsNone(discovery.probe(endpoint))
        with patch.object(discovery, 'read_json', side_effect=OSError):
            self.assertIsNone(discovery.probe(endpoint))
        with patch.object(discovery, 'read_json', return_value={'data': [{'id': 'example-model'}]}) as fetch:
            result = discovery.probe(endpoint)
        fetch.assert_called_once_with(endpoint['base_url'] + '/models')
        self.assertEqual(result['services'], ['Chat'])
        self.assertEqual(result['models'], {'example-model': 'example-model'})
        with patch.object(discovery, 'read_json', return_value={'data': [], 'dotagents':
                          {'protocol': 'dotagents-node', 'schema_version': 1}}):
            self.assertEqual(discovery.probe(endpoint)['protocol'], 'dotagents-node')

    def test_configured_port_and_no_peers_need_no_private_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'config').mkdir()
            config = root / 'config.yaml'
            settings = dict(models=dict(default='host', enabled=['host']),
                            network=dict(gateway_port=9876), applications=dict(images=False, remote_browser=False))
            config.write_text(json.dumps(settings))
            status = dict(BackendState='Running', Peer={})
            result = subprocess.CompletedProcess([], 0, json.dumps(status))
            with patch.object(discovery.subprocess, 'run', return_value=result) as command, \
                    patch.object(discovery, 'probe') as probe:
                self.assertEqual(discovery.discover(root), [])
                probe.assert_not_called()
                self.assertEqual(command.call_args.args[0], ['tailscale', 'status', '--json'])
            for port in (True, 0, 65536, '8888'):
                settings['network']['gateway_port'] = port
                config.write_text(json.dumps(settings))
                with self.assertRaises(ValueError):
                    discovery.discover(root)

    def test_unavailable_tailscale_does_not_block_host(self):
        with patch.object(discovery, 'gateway_port', return_value=8888), \
                patch.object(discovery.subprocess, 'run', side_effect=subprocess.TimeoutExpired('tailscale', 10)):
            self.assertEqual(discovery.discover(Path('/unused')), [])

    def test_discovery_uses_configured_port_and_keeps_only_responding_apis(self):
        status = dict(BackendState='Running', Peer={
            'a': dict(Online=True, HostName='first', TailscaleIPs=['100.64.0.2']),
            'b': dict(Online=True, HostName='second', TailscaleIPs=['100.64.0.3']),
        })
        def fetch(url):
            if url == 'http://100.64.0.2:9876/v1/models':
                return {'data': [{'id': 'example-model'}]}
            raise OSError('closed port')
        with patch.object(discovery, 'gateway_port', return_value=9876), \
                patch.object(discovery.subprocess, 'run', return_value=
                             subprocess.CompletedProcess([], 0, json.dumps(status))), \
                patch.object(discovery, 'read_json', side_effect=fetch):
            endpoints = discovery.discover(Path('/unused'))
        self.assertEqual(len(endpoints), 1)
        self.assertEqual(endpoints[0]['base_url'], 'http://100.64.0.2:9876/v1')

    def test_fresh_discovery_removes_stale_routes_without_changing_old_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.local').mkdir()
            old = root / '.local/inference-endpoints.json'
            old.write_text('private legacy configuration')
            node = dict(id='example', device='example', base_url='http://100.64.0.2:8888/v1',
                        approved=True, access='network', protocol='dotagents-node')
            discovery.save(root, [node])
            self.assertEqual(load_endpoints(root), [node])
            self.assertEqual((root / '.local/discovered-endpoints.json').stat().st_mode & 0o777, 0o600)
            discovery.save(root, [])
            self.assertEqual(load_endpoints(root), [])
            self.assertEqual(old.read_text(), 'private legacy configuration')


if __name__ == '__main__':
    unittest.main()
