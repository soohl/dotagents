"""Metrics trust, freshness, recovery, and dashboard layout checks."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import remote_telemetry as remote


def endpoint():
    return dict(id='test-node', device='test-node', approved=True, access='network',
                url='http://192.0.2.10:8003/v1/system')


def sample():
    return dict(schema_version=1, scope='host', instance_id='one', sample_id=1,
                sample_age_seconds=1, hardware=dict(chip='Test CPU', os='Linux', cores=16,
                cpu_ticks={'busy': 100, 'total': 200}, memory={'used': 8 * 2**30, 'total': 64 * 2**30},
                disk={'used': 100, 'total': 1000}, gpus=[dict(id='gpu-0', name='Example GPU',
                used=18 * 2**30, total=24 * 2**30, utilization=42)]))


class RemoteTests(unittest.TestCase):
    def test_node_configuration_also_supplies_hardware_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.local').mkdir()
            node = dict(id='test-node', device='test-node', approved=True, access='network',
                        base_url='http://192.0.2.10:8001/v1', protocol='dotagents-node')
            (root / '.local/discovered-endpoints.json').write_text(
                json.dumps(dict(schema_version=1, endpoints=[node])))
            expected = endpoint() | {'url': node['base_url'] + '/system'}
            self.assertEqual(remote.load_endpoints(root), [expected])
            (root / '.local/metrics-endpoints.json').write_text(
                json.dumps(dict(schema_version=1, endpoints=[endpoint()])))
            self.assertEqual(remote.load_endpoints(root), [expected])

    def test_power_fields_are_optional_and_keep_draw_separate_from_limit(self):
        data = sample()
        gpu = data['hardware']['gpus'][0]
        self.assertIsNone(remote.validate(data, 100)['gpus'][0]['power_watts'])
        gpu.update(power_watts=351.5, power_limit_watts=350)
        reading = remote.validate(data, 100)['gpus'][0]
        self.assertEqual(reading['power_watts'], 351.5)
        self.assertEqual(reading['power_limit_watts'], 350)
        gpu['power_watts'] = float('nan')
        with self.assertRaises(ValueError):
            remote.validate(data, 100)

    def test_explicit_trust_and_url_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.local').mkdir()
            path = root / '.local/metrics-endpoints.json'
            def load(value):
                path.write_text(json.dumps({'schema_version': 1, 'endpoints': [value]}))
                return remote.load_endpoints(root)
            self.assertEqual(load(endpoint()), [endpoint()])
            for fields in ({'approved': False}, {'url': 'http://8.8.8.8:8003/v1/system'},
                           {'url': 'http://user:pass@192.0.2.10/v1/system'},
                           {'url': 'http://192.0.2.10:8003/processes'}):
                with self.subTest(fields=fields), self.assertRaises(ValueError):
                    load(dict(endpoint(), **fields))

    def test_rejects_stale_or_malformed_samples_preserves_missing_counters(self):
        for path, value in [(('sample_age_seconds',), 13), (('sample_age_seconds',), float('nan')),
                            (('hardware', 'memory', 'used'), -1),
                            (('hardware', 'chip'), '\x1b[2J'),
                            (('hardware', 'cpu_ticks', 'total'), 1)]:
            data = sample()
            field = data
            for key in path[:-1]:
                field = field[key]
            field[path[-1]] = value
            with self.subTest(path=path), self.assertRaises(ValueError):
                remote.validate(data, 100)
        data = sample()
        data['hardware']['memory']['used'] = None
        data['hardware']['cpu_ticks'] = None
        reading = remote.validate(data, 100)
        self.assertIsNone(reading['memory']['used'])
        self.assertIsNone(reading['cpu_ticks'])
        self.assertEqual(reading['gpus'][0]['utilization'], 42)

    def test_duplicate_failure_backoff_and_restart_reset(self):
        peer = remote.Peer(endpoint())
        data = sample()
        first = peer.poll(clock=lambda: 100, fetch=lambda url: copy.deepcopy(data))
        self.assertTrue(first['hardware']['reset'])
        self.assertIsNone(peer.poll(clock=lambda: 100.5, fetch=lambda url: self.fail('Unexpected poll')))
        self.assertIsNone(peer.poll(clock=lambda: 101, fetch=lambda url: copy.deepcopy(data)))
        self.assertEqual(peer.failures, 0)
        self.assertEqual(peer.next_poll, 102)
        failed = peer.poll(clock=lambda: 113, fetch=lambda url: copy.deepcopy(data))
        self.assertEqual(failed['state'], 'disconnected')
        failed = peer.poll(clock=lambda: 114, fetch=lambda url: copy.deepcopy(data))
        self.assertEqual(peer.next_poll, 116)
        data['instance_id'] = 'restarted'
        recovered = peer.poll(clock=lambda: 116, fetch=lambda url: copy.deepcopy(data))
        self.assertTrue(recovered['hardware']['reset'])
        self.assertEqual(peer.failures, 0)
        data['sample_id'] = 2
        self.assertFalse(peer.poll(clock=lambda: 117, fetch=lambda url: data)['hardware']['reset'])


class RemoteLayoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_machines_and_panels_fit_and_failure_keeps_local_samples(self):
        from tui import DotAgents, RemoteHardware
        from charts import UsageChart, InferenceChart, MemoryLegend
        from textual.widgets import Footer
        observation = dict(models=[], checks=[], devices=[], inference_services=[], applications=[])
        for size in ((80, 24), (120, 42)):
            with self.subTest(size=size), patch('remote_telemetry.load_endpoints', return_value=[endpoint()]), \
                    patch('tui.control_plane.snapshot', return_value=observation), \
                    patch.object(DotAgents, 'refresh_hardware'), patch.object(remote.Peer, 'poll', return_value=None):
                app = DotAgents()
                async with app.run_test(size=size) as pilot:
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                    system = app.query_one('#system-panel').region
                    charts = list(app.query(UsageChart))
                    self.assertEqual(len(charts), 4)
                    for chart in charts:
                        legend = chart.parent.query_one(MemoryLegend)
                        self.assertIn(chart.label, legend.render().plain)
                        if chart.display:
                            self.assertGreaterEqual(chart.size.height, 3)
                            self.assertTrue(system.contains_region(chart.region), str(chart.region))
                            self.assertIn('100│', chart.render().plain)
                    speeds = list(app.query(InferenceChart))
                    self.assertEqual(len(speeds), 2)
                    for chart in speeds:
                        self.assertGreaterEqual(chart.size.height, 3)
                        self.assertTrue(system.contains_region(chart.region), str(chart.region))
                        self.assertIn('Prefill', chart.render().plain)
                        self.assertIn('Decode', chart.render().plain)
                    local = app.query_one('#ram-chart', UsageChart)
                    local.sample(100, .5, 'Local')
                    card = app.query_one(RemoteHardware)
                    hardware = remote.validate(sample(), 100)
                    hardware['reset'] = True
                    card.show_reading(dict(state='connected', hardware=hardware, sampled_at=100))
                    vram = list(card.query(UsageChart))[1]
                    self.assertEqual(vram.samples[-1][1], .75)
                    card.show_reading(dict(state='disconnected', sampled_at=105, detail='Offline'))
                    self.assertIsNone(vram.samples[-1][1])
                    self.assertEqual(local.samples[-1][1], .5)
                    for name in ('stack', 'activity', 'inference'):
                        region = app.query_one('#' + name + '-panel').region
                        if size[1] >= 40:
                            self.assertTrue(app.screen.region.contains_region(region))
                        self.assertGreaterEqual(region.height, 4)
                        self.assertGreaterEqual(region.y, system.bottom)
                    self.assertEqual(app.query_one('#dashboard').region.bottom, app.query_one(Footer).region.y)
