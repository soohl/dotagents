"""Endpoint observations must not invent serving models or grant management."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import inference_observations as observations
from inference_view import bindings, transport, endpoint_address


def endpoint():
    return dict(id='node-a-chat', device='node-a', base_url='http://100.80.0.2:8001/v1',
                approved=True, access='network', services=['Chat'], engine='Example',
                execution='docker', models={'expected-model': 'planned-profile'})


class ObservationTests(unittest.TestCase):
    def test_existing_node_protocol_remains_readable_after_project_rename(self):
        node = dict(id='node', device='node', base_url='http://100.80.0.2:8001/v1',
                    approved=True, access='network', protocol='sealedllm-node')
        response = dict(sealedllm=dict(schema_version=1, protocol='sealedllm-node'), data=[
            dict(id='saved-model', sealedllm=dict(name='Saved model', engine='Example',
                 execution='docker', service='Chat', installed=True, loadable=True,
                 ready=True, loaded=True, state='loaded', context_window=131072))])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.local').mkdir()
            (root / '.local/discovered-endpoints.json').write_text(
                json.dumps(dict(schema_version=1, endpoints=[node])))
            self.assertEqual(observations.load_endpoints(root), [node])
            with patch.object(observations, 'read_json', return_value=response):
                service, = observations.observe(node)
                chat, = observations.chat_endpoints(root)
            self.assertEqual(service['model_ids'], ['saved-model'])
            self.assertEqual(service['state'], 'ready')
            self.assertEqual(chat['models'], {'saved-model': 'Saved model'})
        self.assertNotIn('dotagents', response['data'][0])

    def test_local_catalog_is_required_even_when_profiles_and_health_exist(self):
        base = 'http://127.0.0.1:8001/v1'
        models = [dict(id='configured', endpoint=base, name='Configured name',
                       runtime='loaded', activity='loaded', engine='old', artifacts='present'),
                  dict(id='planned', status='planned', name='Never advertised')]
        pins = {'configured': {'api_model': 'advertised'}}
        inference = {'endpoints': {'configured': {'endpoint': base, 'state': 'ready'}}}
        for response in [{'data': []}, {}, {'data': [{'name': 'invalid'}]}, OSError('offline')]:
            with self.subTest(response=response), patch.object(observations, 'read_json',
                    side_effect=response if isinstance(response, OSError) else None,
                    return_value=response):
                services = observations.local_services(inference, models, pins)
            row, = bindings(dict(models=models, inference_services=services))
            self.assertEqual(row['model'], '—')
            self.assertNotIn('Unassigned', row['device'])
        with patch.object(observations, 'read_json', return_value={'data': [
                {'id': 'advertised', 'name': 'API name', 'engine': 'new'},
                {'id': 'new-model', 'name': 'New model', 'engine': 'new'}]}):
            services = observations.local_services(inference, models, pins)
        rows = bindings(dict(models=models, inference_services=services))
        self.assertEqual([r['model'] for r in rows], ['advertised', 'new-model'])
        self.assertEqual([r['model_name'] for r in rows], ['API name', 'New model'])
        self.assertEqual([r['engine'] for r in rows], ['new', 'new'])
        self.assertEqual([r['state'] for r in rows], ['loaded', 'unknown'])
        self.assertEqual(rows[0]['key'], 'configured')

    def test_profiles_alone_never_create_inference_rows(self):
        self.assertEqual(bindings(dict(models=[dict(id='local', status='available'),
                                              dict(id='planned', status='planned')],
                                       inference_services=[])), [])

    def test_node_catalog_supplies_engines_names_and_stopped_models(self):
        node = dict(id='node-a', device='node-a', base_url='http://100.80.0.2:8001/v1',
                    approved=True, access='network', protocol='dotagents-node')
        def item(key, engine, loaded, ready):
            return dict(id=key, dotagents=dict(name=key + ' full name', engine=engine,
                        execution='docker', service='Chat', installed=True, loadable=True,
                        ready=ready, loaded=loaded, state='loaded' if loaded else 'unknown',
                        context_window=262144, metrics={}))
        response = dict(dotagents=dict(schema_version=1, protocol='dotagents-node'),
                        data=[item('new-model', 'New engine', True, True),
                              item('stopped-model', 'Other engine', None, False)])
        with patch.object(observations, 'read_json', return_value=response) as read:
            rows = bindings(dict(models=[], devices=[], inference_services=observations.observe(node)))
        self.assertEqual([r['engine'] for r in rows], ['New engine', 'Other engine'])
        self.assertEqual([r['model_name'] for r in rows], ['new-model full name', 'stopped-model full name'])
        self.assertEqual([r['state'] for r in rows], ['loaded', 'unknown'])
        self.assertEqual([r['connection_state'] for r in rows], ['connected', 'connected'])
        self.assertIn('Available to load: True', rows[1]['detail'])
        read.assert_called_once_with(node['base_url'] + '/models')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.local').mkdir()
            (root / '.local/discovered-endpoints.json').write_text(
                json.dumps(dict(schema_version=1, endpoints=[node])))
            self.assertEqual(observations.load_endpoints(root), [node])
            with patch.object(observations, 'read_json', return_value=response):
                chat, = observations.chat_endpoints(root)
            self.assertEqual(chat['contexts']['new-model'], 262144)
            self.assertEqual(chat['models']['stopped-model'], 'stopped-model full name')
        response['data'][0]['dotagents']['loaded'] = False
        with patch.object(observations, 'read_json', return_value=response):
            service, = observations.observe(node)
        self.assertEqual(service['state'], 'unknown')
        self.assertEqual(service['model_ids'], [])

    def test_strata_health_reports_residency_without_inventing_activity(self):
        config = endpoint() | {'health': 'strata', 'engine': 'Strata'}
        for loaded, expected in [(True, 'loaded'), (False, 'unloaded'), (None, 'unknown'),
                                 ('true', 'unknown')]:
            with self.subTest(loaded=loaded), patch.object(observations, 'read_json', side_effect=[
                    {'data': [{'id': 'chat'}]},
                    {'status': 'ok', 'service': 'strata', 'model': 'chat', 'loaded': loaded}]):
                services = observations.observe(config)
            row, = bindings(dict(models=[], devices=[], inference_services=services))
            self.assertEqual(row['connection_state'], 'connected')
            self.assertEqual(row['engine'], 'Strata')
            self.assertEqual(row['state'], expected)
            self.assertEqual(row['speed_metrics'], {})
            self.assertIn('Active requests and inference rates are not reported', row['detail'])
        for health in [OSError('unavailable'),
                       {'status': 'ok', 'service': 'strata', 'model': 'other', 'loaded': True},
                       {'status': 'ok', 'service': 'other', 'model': 'chat', 'loaded': True}]:
            with self.subTest(health=health), patch.object(observations, 'read_json', side_effect=[
                    {'data': [{'id': 'chat'}]}, health]):
                services = observations.observe(config)
            row, = bindings(dict(models=[], devices=[], inference_services=services))
            self.assertEqual(row['state'], 'unknown')
            self.assertEqual(row['connection_state'], 'connected')

    def test_worker_timings_stay_bound_to_reported_model_and_service(self):
        config = endpoint() | {'health': 'dotagents-chat'}
        with patch.object(observations, 'read_json', side_effect=[
                {'data': [{'id': 'chat'}]},
                {'status': 'ok', 'model': 'chat', 'worker': {'loaded': True, 'status': 'idle',
                 'last_request': {'prefill_tps': 500, 'decode_tps': 40, 'prompt': 'private'}}}]):
            services = observations.observe(config)
        row, = bindings(dict(models=[], devices=[], inference_services=services))
        self.assertEqual(row['speed_metrics'], {'prefill_tps': 500, 'decode_tps': 40})
        self.assertNotIn('private', str(services))

    def test_chat_health_drives_residency_and_work_not_api_readiness(self):
        config = endpoint() | {'health': 'dotagents-chat'}
        for loaded, phase, residency, activity in [(True, 'idle', 'loaded', 'loaded'),
                                                  (True, 'generating', 'loaded', 'running'),
                                                  (False, 'idle', 'standby', 'unloaded')]:
            with patch.object(observations, 'read_json', side_effect=[
                    {'data': [{'id': 'chat'}]},
                    {'status': 'ok', 'model': 'chat', 'worker': {'loaded': loaded, 'status': phase}}]):
                services = observations.observe(config)
            row, = bindings(dict(models=[], devices=[], inference_services=services))
            self.assertEqual(row['connection_state'], 'connected')
            self.assertEqual(row['model_state'], residency)
            self.assertEqual(row['state'], activity)
        with patch.object(observations, 'read_json', side_effect=[
                {'data': [{'id': 'chat'}]}, OSError('status unavailable')]):
            services = observations.observe(config)
        row, = bindings(dict(models=[], devices=[], inference_services=services))
        self.assertEqual(row['model_state'], 'unknown')
        self.assertEqual(row['state'], 'unknown')
        self.assertEqual(row['connection_state'], 'connected')

    def test_service_group_uses_loaded_model_first_and_preserves_each_endpoint(self):
        models = [dict(id=name, endpoint=f'http://127.0.0.1:{port}/v1', runtime=state,
                       status='available', artifacts='present', engine='DS4', kind='chat')
                  for name, port, state in [('qwen', 8000, 'standby'), ('deepseek', 8001, 'loaded')]]
        snapshot = dict(models=models, inference_services=[dict(
            name='Chat', role='service', node={'location': 'local'}, state='running',
            endpoints=[model['endpoint'] for model in models],
                        model_ids=[m['id'] for m in models], catalog_models={m['id']: m['id'] for m in models}, engine='DS4', execution='native', detail='test')])
        rows = bindings(snapshot)
        self.assertEqual([r['key'] for r in rows], ['deepseek', 'qwen'])
        self.assertEqual([r['service_display'] for r in rows], ['Chat', ''])
        self.assertEqual([r['address'] for r in rows], ['127.0.0.1:8001', '127.0.0.1:8000'])
        self.assertEqual(endpoint_address('http://[fd7a:115c:a1e0::2]:8002/v1'), '[fd7a:115c:a1e0::2]:8002')
        self.assertEqual(endpoint_address(None), '—')

    def test_local_rows_identify_each_models_engine(self):
        models = [dict(id=key, endpoint=f'http://127.0.0.1:{port}/v1', runtime='loaded',
                       status='available', artifacts='present', engine=engine, kind='chat')
                  for key, port, engine in [('qwen', 8000, 'ds4'), ('qwen-omlx', 8002, 'omlx')]]
        services = [dict(name='Chat', role='service', node={'location': 'local'}, state='ready',
                        endpoints=[model['endpoint'] for model in models],
                        model_ids=[m['id'] for m in models], catalog_models={m['id']: m['id'] for m in models}, engine='DS4 / oMLX',
                        execution='native', detail='Test service')]
        rows = bindings(dict(models=models, inference_services=services))
        self.assertEqual({row['model']: row['engine'] for row in rows},
                         {'qwen': 'ds4', 'qwen-omlx': 'omlx'})

    def test_transport_uses_endpoint_address_not_peer_direct_path(self):
        node = {'location': 'remote'}
        for url, expected in [('http://100.80.0.2:8001/v1', 'Tailscale'),
                              ('http://[fd7a:115c:a1e0::2]:8001/v1', 'Tailscale'),
                              ('http://192.168.1.20:8001/v1', 'LAN'),
                              ('https://unresolved.example/v1', 'Unknown')]:
            with self.subTest(url=url):
                self.assertEqual(transport([url], node), expected)
        self.assertEqual(transport([], {'location': 'local'}), 'Host')

    def test_device_grouping_keeps_each_service_connection_separate(self):
        with patch.object(observations, 'read_json', return_value={'data': [{'id': 'model'}]}):
            a = observations.observe(endpoint())
            b = observations.observe(endpoint() | {'id': 'node-b', 'device': 'node-b'})
            images = observations.observe(endpoint() | {'id': 'node-a-images',
                                                        'services': ['Image generation', 'Image editing']})
        images[0]['state'] = 'unknown'
        rows = bindings({'models': [], 'devices': [], 'inference_services': a + b + images})
        self.assertEqual([r['device'] for r in rows], ['node-a', 'node-a', 'node-b'])
        self.assertEqual([r['device_display'] for r in rows], ['node-a', '', 'node-b'])
        self.assertEqual([r['connection_state'] for r in rows],
                         ['connected', 'disconnected', 'connected'])
        self.assertEqual([r['service'] for r in rows], ['Chat', 'Image', 'Chat'])

    def test_connected_devices_sort_before_offline_devices_and_unassigned_profiles(self):
        with patch.object(observations, 'read_json', return_value={'data': [{'id': 'model'}]}):
            offline = observations.observe(endpoint() | {'device': 'offline', 'id': 'offline'})
            online = observations.observe(endpoint() | {'device': 'online', 'id': 'online'})
        offline[0]['state'] = 'unknown'
        snapshot = dict(models=[dict(id='planned', status='planned', engine='test', kind='chat',
                                     name='Planned', artifacts='none')],
                        inference_services=offline + online, devices=[
                            dict(name='a-offline', address='100.80.0.4', connection='Offline', trust='Discovered'),
                            dict(name='z-online', address='100.80.0.5', connection='Direct', trust='Discovered')])
        rows = bindings(snapshot)
        self.assertEqual([row['device'] for row in rows],
                         ['online', 'z-online', 'offline', 'a-offline'])
        self.assertEqual([row['connection_state'] for row in rows],
                         ['connected', 'connected', 'disconnected', 'disconnected'])
        online[0]['state'] = 'unknown'
        offline[0]['state'] = 'ready'
        self.assertEqual(bindings(snapshot)[0]['device'], 'offline')

    def test_catalog_reports_actual_models_and_never_falls_back_to_config(self):
        with patch.object(observations, 'read_json', return_value={'data': [{'id': 'actual-model'}]}) as read:
            row, = observations.observe(endpoint())
        self.assertEqual(row['model_ids'], ['actual-model'])
        self.assertEqual(row['model_states'], {'actual-model': 'unknown'})
        read.assert_called_once_with('http://100.80.0.2:8001/v1/models')
        for response in ({}, {'data': [{'name': 'wrong'}]}, {'data': 'invalid'}):
            with patch.object(observations, 'read_json', return_value=response):
                row, = observations.observe(endpoint())
            self.assertEqual(row['model_ids'], [])
            self.assertEqual(row['state'], 'unknown')
        with patch.object(observations, 'read_json', side_effect=OSError('unreachable')):
            row, = observations.observe(endpoint())
        self.assertEqual(row['model_ids'], [])

    def test_image_api_readiness_does_not_mean_worker_is_loaded(self):
        config = endpoint() | {'services': ['Image generation', 'Image editing'], 'health': 'dotagents-image'}
        for loaded, phase, model_state, activity in [(False, 'idle', 'standby', 'unloaded'),
                                                     (True, 'idle', 'loaded', 'loaded'),
                                                     (False, 'loading', 'standby', 'unloaded'),
                                                     (True, 'generating', 'loaded', 'running')]:
            with self.subTest(phase=phase, loaded=loaded), patch.object(observations, 'read_json', side_effect=[
                    {'data': [{'id': 'image'}]},
                    {'status': 'ok', 'model': 'image', 'worker': {'loaded': loaded, 'status': phase}}]):
                rows = observations.observe(config)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['name'], 'Image')
            self.assertTrue(all(row['state'] == 'ready' and row['model_states']['image'] == model_state
                                and row['model_activity']['image'] == activity for row in rows))
            self.assertIn('capacity is not verified', rows[0]['detail'])

    def test_observations_require_explicit_configuration_and_trust(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(observations, 'read_json') as read:
                self.assertEqual(observations.remote_services(root), [])
                read.assert_not_called()
            (root / '.local').mkdir()
            path = root / '.local/discovered-endpoints.json'
            for override in ({'approved': False}, {'access': 'discovered'},
                             {'base_url': 'http://8.8.8.8/v1'},
                             {'base_url': 'http://user:password@100.80.0.2/v1'},
                             {'base_url': 'http://100.80.0.2/v1?token=secret'},
                             {'services': ['Management']}, {'models': []}):
                path.write_text(json.dumps({'schema_version': 1, 'endpoints': [endpoint() | override]}))
                with self.subTest(override=override), self.assertRaises(ValueError):
                    observations.load_endpoints(root)
            path.write_text(json.dumps({'schema_version': 1, 'endpoints': [endpoint()]}))
            self.assertEqual(observations.load_endpoints(root), [endpoint()])

    def test_same_model_on_two_devices_keeps_distinct_rows_without_local_actions(self):
        with patch.object(observations, 'read_json', return_value={'data': [{'id': 'qwen'}]}):
            services = observations.observe(endpoint())
            services += observations.observe(endpoint() | {'id': 'node-b-chat', 'device': 'node-b',
                                                           'base_url': 'http://100.80.0.3:8001/v1'})
        snapshot = {'models': [], 'devices': [], 'inference_services': services}
        rows = bindings(snapshot)
        self.assertEqual(len({row['key'] for row in rows}), 2)
        self.assertEqual([row['model'] for row in rows], ['qwen', 'qwen'])
        self.assertIn('node-a', rows[0]['device'])
        self.assertIn('node-b', rows[1]['device'])
        named = copy.deepcopy(snapshot)
        named['models'] = [dict(id='qwen', name='Qwen 3.8 Flash Next', status='planned',
                                kind='chat', engine='ds4', artifacts='none')]
        named['inference_services'][0]['catalog_models'] = {'qwen': 'qwen'}
        named_rows = bindings(named)
        self.assertEqual([row['model_name'] for row in named_rows], ['Qwen 3.8 Flash Next', 'qwen'])
        self.assertEqual([row['key'] for row in named_rows], [row['key'] for row in rows])
        self.assertEqual([row['model'] for row in named_rows], ['qwen', 'qwen'])
        failed = copy.deepcopy(snapshot)
        failed['inference_services'][1].update(model_ids=[], state='unknown')
        self.assertEqual([row['model'] for row in bindings(failed)], ['qwen', '—'])

    def test_discovery_alone_does_not_assign_a_model_or_service(self):
        rows = bindings({'models': [], 'inference_services': [], 'devices': [
            dict(name='node-a', address='100.80.0.2', connection='Direct (last reported)',
                 trust='Discovered; not enrolled')]})
        self.assertEqual(rows[0]['service'], '—')
        self.assertEqual(rows[0]['model'], '—')
