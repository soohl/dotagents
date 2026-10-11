"""Headless terminal checks. Opening screens must not launch workers."""
import asyncio
from pathlib import Path
import sys
import time
import threading
from types import SimpleNamespace
import unittest
import asyncio
import os
import tempfile
from unittest.mock import AsyncMock, PropertyMock, patch
from rich.color import Color
from rich.console import Console

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import control_plane
from tui import DotAgents, Status, RemoteHardware, plain_output
from charts import UsageChart, InferenceChart, MemoryLegend, label_space
from dashboard_view import InferenceColumn, stack_services, stack_addresses, docker_state, hardware_label
from inference_speeds import SpeedHistory
from textual.geometry import Size
from textual.widgets import Button, DataTable, Footer, Header, Input, Select, Tabs, TabbedContent


def observation(loaded=()):
    models = control_plane.model_rows()
    for model in models:
        if model['id'] in loaded:
            model.update(runtime='loaded', activity='loaded')
    endpoints = {m['id']: {'endpoint': m['endpoint'], 'state': 'offline', 'active_model': None}
                 for m in models if m.get('endpoint')}
    services = [dict(name='Chat', role='service', state='ready' if loaded else 'offline',
                     node={'location': 'local'}, endpoints=[m['endpoint'] for m in models if m.get('endpoint')],
                     model_ids=[m['id'] for m in models if m.get('endpoint')],
                     catalog_models={m['id']: m['id'] for m in models if m.get('endpoint')},
                     engine='—', execution='native', detail='Test API advertisement')]
    return {'platform': 'mac-metal', 'checks': [
        {'name': 'New device API security', 'state': 'planned', 'detail': 'Pending'},
        {'name': 'Stack tunnel', 'state': 'disabled', 'detail': 'Stack routes are off'}],
        'containers': [{'name': 'caddy', 'container_name': 'dotagents-caddy-1', 'state': 'running', 'health': 'healthy',
                        'published': ['127.0.0.1:3000']},
                       {'name': 'chat', 'container_name': 'dotagents-chat-1', 'state': 'running', 'health': 'healthy'}],
        'models': models, 'devices': [],
        'inference_services': services}


class DashboardLifecycleTests(unittest.IsolatedAsyncioTestCase):


    async def test_activity_shares_web_session_and_stop_keeps_ui_alive(self):
        state = observation()
        state['containers'].append(dict(name='agent', state='running', published=['127.0.0.1:3002']))
        with patch.object(control_plane, 'snapshot', return_value=state), \
                patch.object(DotAgents, 'start_inference'), patch.object(DotAgents, 'refresh_inference'), \
                patch.object(DotAgents, 'refresh_hardware'), patch('tui.ServiceLogs.sync'), \
                patch('remote_telemetry.load_endpoints', return_value=[]), \
                patch('harness_service.web_command') as command:
            app = DotAgents()
            async with app.run_test(size=(120, 40)):
                await app.workers.wait_for_complete()
                app.run_agent('Review the code')
                await app.workers.wait_for_complete()
                command.assert_called_with(app.root, 'Review the code')
                app.stop_agent()
                await app.workers.wait_for_complete()
                command.assert_called_with(app.root)
                app.action_quit()
                await app.workers.wait_for_complete()

    async def test_agent_input_accepts_q_and_quit_closes_dashboard(self):
        with patch.object(control_plane, 'snapshot', return_value=observation()), \
                patch.object(DotAgents, 'start_inference'), patch('tui.ServiceLogs.sync'), \
                patch('remote_telemetry.load_endpoints', return_value=[]):
            app = DotAgents()
            async with app.run_test(size=(120, 40)) as pilot:
                await app.workers.wait_for_complete()
                field = app.query_one('#agent-task', Input)
                field.focus()
                await pilot.press('q')
                self.assertEqual(field.value, 'q')
                self.assertFalse(app.closing)
                await pilot.press('escape', 'q')
                self.assertTrue(app.closing)

    async def test_quit_shows_progress_and_keeps_the_ui_responsive_during_shutdown(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def slow_stop():
            started.set()
            await release.wait()
        with patch.object(control_plane, 'snapshot', return_value=observation()), \
                patch.object(DotAgents, 'start_inference'), \
                patch('tui.ServiceLogs.sync'), \
                patch('remote_telemetry.load_endpoints', return_value=[]):
            app = DotAgents()
            app.gateway_job.stop = AsyncMock(side_effect=slow_stop)
            try:
                async with app.run_test(size=(120, 40)) as pilot:
                    await app.workers.wait_for_complete()
                    app.query_one('#inference').focus()
                    await asyncio.wait_for(pilot.press('q'), timeout=2)
                    await asyncio.wait_for(started.wait(), timeout=2)
                    self.assertTrue(app.closing)
                    self.assertTrue(app.is_running)
                    self.assertIn('Stopping inference', str(app.query_one('#operation').render()))
                    await asyncio.wait_for(pilot.press('q'), timeout=2)
                    app.gateway_job.stop.assert_awaited_once()
                    release.set()
                    await app.workers.wait_for_complete()
            finally:
                release.set()

    async def test_quit_stops_the_dashboard_gateway_and_engine(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.env').touch()
            (root / 'src').mkdir()
            (root / 'config').symlink_to(ROOT / 'config', target_is_directory=True)
            (root / 'src/dashboard_inference.py').write_text('''import pathlib, signal, subprocess, sys, time
root = pathlib.Path(__file__).resolve().parents[1]
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
def stop(*args):
    child.wait(timeout=5)
    (root / 'stopped').touch()
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
(root / 'ready').write_text(str(child.pid))
while True: time.sleep(1)
''')
            state = observation()
            state['models'] = [dict(state['models'][0], platform='mac-metal', status='available',
                                    artifacts='Present; hash not checked')]
            with patch.object(control_plane, 'snapshot', return_value=state), \
                    patch('tui.ServiceLogs.sync'), \
                    patch.object(DotAgents, 'refresh_hardware'), \
                    patch.object(DotAgents, 'refresh_inference'), \
                    patch('remote_telemetry.load_endpoints', return_value=[]):
                app = DotAgents(root)
                app.verified = True
                async with app.run_test(size=(120, 40)) as pilot:
                    async def ready():
                        while not (root / 'ready').exists():
                            await asyncio.sleep(.02)
                    await asyncio.wait_for(ready(), timeout=5)
                    child = int((root / 'ready').read_text())
                    await pilot.press('q')
                self.assertFalse(app.gateway_job.running)
                self.assertEqual(app.gateway_job.process.returncode, 0)
                self.assertTrue((root / 'stopped').exists())
                with self.assertRaises(ProcessLookupError):
                    os.kill(child, 0)


class TerminalTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_api_count_matches_stack_for_a_multi_model_node(self):
        result = observation()
        for model, capability in [('chat-a', 'Chat'), ('chat-b', 'Chat'), ('image', 'Image')]:
            result['inference_services'].append(dict(
                id='tower:' + model, name=capability, role='service', state='ready',
                node={'label': 'tower', 'location': 'remote'},
                endpoints=['http://100.64.0.2:8001/v1'], model_ids=[model],
                model_states={model: 'unknown'}, engine='Example', detail='Node catalog'))
        with patch.object(control_plane, 'snapshot', return_value=result), \
                patch.object(DotAgents, 'start_inference'), patch.object(DotAgents, 'refresh_inference'), \
                patch('tui.ServiceLogs.sync'), patch('remote_telemetry.load_endpoints', return_value=[]):
            app = DotAgents()
            async with app.run_test(size=(240, 40)):
                await app.workers.wait_for_complete()
                self.assertEqual(app.query_one('#inference-panel').border_subtitle,
                                 'Model APIs: 1/2 connected')
                self.assertIn('1/2 APIs available.', app.row_details['stack']['Model APIs'])
                for row in result['inference_services'][1:]:
                    row['state'] = 'unknown'
                app.show_snapshot(result)
                self.assertEqual(app.query_one('#inference-panel').border_subtitle,
                                 'Model APIs: 0/2 connected')
                self.assertIn('0/2 APIs available.', app.row_details['stack']['Model APIs'])

    def test_gateway_address_uses_shared_tunnel_namespace(self):
        result = observation()
        result['containers'][0]['published'] = []
        result['containers'].append(dict(name='tailscale', state='running',
                                         published=['127.0.0.1:3000']))
        result['harness'] = {'backend': 'harness'}
        addresses = stack_addresses(result)
        self.assertEqual(addresses['Gateway'], ['127.0.0.1:3000'])
        self.assertEqual(addresses['Chat'], addresses['Gateway'])
        self.assertEqual(addresses['Agent'], addresses['Gateway'])
        self.assertEqual(addresses['Tunnel'], [])

    def test_harness_services_show_their_individual_browser_ports(self):
        result = observation()
        result['harness'] = {'backend': 'harness'}
        result['containers'].extend([
            dict(name='chat', state='running', published=['127.0.0.1:3002']),
            dict(name='agent', state='running', published=['127.0.0.1:3003'])])
        addresses = stack_addresses(result)
        self.assertEqual(addresses['Chat'], ['127.0.0.1:3002'])
        self.assertEqual(addresses['Agent'], ['127.0.0.1:3003'])
        self.assertEqual(addresses['Gateway'], ['127.0.0.1:3000'])

    def test_harness_and_authentication_have_independent_availability(self):
        result = observation()
        result['harness'] = dict(backend='harness', services={'chat': {}, 'agent': {}}, activity={}, detail='No active agents.')
        agent = dict(name='agent', state='running', health='healthy')
        chat = dict(name='chat', state='running', health='healthy')
        auth = dict(name='authelia', state='running', health='healthy')
        result['containers'].extend([chat, agent, auth])

        def states():
            return {name: state for name, state, _ in stack_services(result)}

        self.assertEqual(list(states()), ['Tunnel', 'Gateway', 'Authentication',
                                         'Chat', 'Agent', 'Image workspace', 'Model APIs'])
        self.assertEqual(states()['Agent'], 'ready')
        self.assertEqual(states()['Authentication'], 'ready')
        self.assertEqual(states()['Model APIs'], 'disabled')
        self.assertEqual(stack_addresses(result)['Agent'], ['127.0.0.1:3000'])
        auth['health'] = 'unhealthy'
        self.assertEqual(states()['Authentication'], 'disabled')
        self.assertEqual(states()['Agent'], 'ready')
        result['harness']['services']['agent']['active'] = 1
        self.assertEqual(states()['Agent'], 'running')
        self.assertEqual(states()['Chat'], 'ready')
        agent['state'] = 'exited'
        self.assertEqual(states()['Agent'], 'disabled')

    def test_stack_addresses_use_application_ports_and_keep_api_routes_separate(self):
        result = observation()
        result['containers'].extend([
            dict(name='agent', state='running', published=['127.0.0.1:3002']),
            dict(name='image-web', state='running', published=['127.0.0.1:3001']),
        ])
        result['inference_services'].append(dict(role='service', endpoints=['http://100.64.0.2:8001/v1']))
        addresses = stack_addresses(result)
        self.assertEqual(addresses['Chat'], ['127.0.0.1:3000'])
        self.assertEqual(addresses['Agent'], ['127.0.0.1:3002'])
        self.assertEqual(addresses['Image workspace'], ['127.0.0.1:3001'])
        self.assertIn('100.64.0.2:8001', addresses['Model APIs'])
        self.assertEqual(addresses['Authentication'], [])
        self.assertEqual(addresses['Tunnel'], [])
        result['containers'][-2]['state'] = 'exited'
        self.assertEqual(stack_addresses(result)['Agent'], ['127.0.0.1:3000'])

    def test_service_summaries_and_docker_use_the_requested_states(self):
        result = observation()
        image = dict(id='image', name='Image', role='service', state='ready',
                     model_activity={'image': 'loaded'}, detail='Image API responds.')
        result['inference_services'].append(image)
        def states():
            return {name: state for name, state, _ in stack_services(result)}
        self.assertEqual(states()['Chat'], 'ready')
        self.assertEqual(states()['Image workspace'], 'disabled')
        self.assertEqual(states()['Model APIs'], 'ready')
        result['containers'].append(dict(name='image-web', state='running', health='healthy'))
        self.assertEqual(states()['Image workspace'], 'ready')
        image['model_activity']['image'] = 'running'
        self.assertEqual(states()['Image workspace'], 'ready')
        self.assertEqual(states()['Model APIs'], 'running')
        result['harness'] = dict(services={'agent': {'active': 1}})
        result['containers'].append(dict(name='agent', state='running', health='healthy'))
        chat = dict(id='chat', name='Chat', role='service', state='ready',
                    model_activity={'qwen': 'running'}, detail='Harness uses this Chat API.')
        result['inference_services'].append(chat)
        self.assertEqual(states()['Agent'], 'running')
        self.assertEqual(states()['Chat'], 'ready')
        result['containers'][1]['state'] = 'exited'
        self.assertEqual(states()['Chat'], 'disabled')
        self.assertEqual(states()['Model APIs'], 'running')
        image['state'] = 'offline'
        chat['state'] = 'offline'
        self.assertEqual(states()['Image workspace'], 'ready')
        self.assertEqual(states()['Model APIs'], 'disabled')
        self.assertLessEqual(set(states().values()), {'ready', 'running', 'disabled'})
        for process, health, expected in [('running', 'healthy', 'healthy'),
                                          ('running', '—', 'healthy'),
                                          ('running', 'starting', 'unhealthy'),
                                          ('running', 'unhealthy', 'unhealthy'),
                                          ('unknown', 'unknown', 'unhealthy'),
                                          ('exited', 'healthy', 'disabled')]:
            self.assertEqual(docker_state(dict(state=process, health=health)), expected)


    async def test_omlx_rates_reach_chart_without_activity_readers(self):
        state = observation(loaded=('qwen-omlx',))
        model = next(model for model in state['models'] if model['id'] == 'qwen-omlx')
        model['speed_metrics'] = dict(prefill_tps=385.66, decode_tps=55.89,
                                      measured_at=time.time()-300)
        with patch.object(control_plane, 'snapshot', return_value=state):
            app = DotAgents()
            async with app.run_test(size=(260, 40)):
                await app.workers.wait_for_complete()
                table = app.query_one('#inference', DataTable)
                self.assertAlmostEqual(app.speed_histories['qwen-omlx'].value('prefill', time.monotonic()), 385.66)
                self.assertAlmostEqual(app.speed_histories['qwen-omlx'].value('decode', time.monotonic()), 55.89)
                history = app.speed_histories['qwen-omlx']
                self.assertIs(app.query_one(InferenceChart).history, history)
                app.refresh_speed_rows()
                self.assertEqual(len(history.samples['prefill']), 1)
                self.assertEqual(history.chart_samples('decode', time.monotonic()+120)[-1][1], 55.89)
                self.assertNotIn('prefill_tps', app.speed_histories.get('qwen', SpeedHistory()).latest)

    async def test_monitor_keeps_metrics_fast_and_slows_service_probes(self):
        endpoint = dict(device='test', url='http://100.64.0.2:8003/v1/system')
        with patch.object(control_plane, 'snapshot', return_value=observation()), \
                patch('remote_telemetry.load_endpoints', return_value=[endpoint]), \
                patch.object(DotAgents, 'refresh_remote_hardware'):
            app = DotAgents()
            with patch.object(app, 'set_interval', wraps=app.set_interval) as intervals:
                async with app.run_test(size=(120, 40)):
                    await app.workers.wait_for_complete()
                    refreshes = [call.args[0] for call in intervals.call_args_list
                                 if call.args[1] in (app.refresh_state, app.refresh_inference,
                                                    app.refresh_hardware, app.refresh_speed_rows,
                                                    app.refresh_remote_hardware)]
                    self.assertEqual(refreshes, [5, 1, 1, 1, 1])

    async def test_inference_hardware_joins_device_and_clears_stale_usage(self):
        state = observation()
        state['inference_services'][0].update(
            state='ready', node={'label': 'gpu-box', 'location': 'remote'},
            endpoints=['http://100.64.0.2:8001/v1'], model_ids=['qwen', 'deepseek'],
            model_states={'qwen': 'loaded', 'deepseek': 'loaded'})
        with patch.object(control_plane, 'snapshot', return_value=state):
            app = DotAgents()
            async with app.run_test(size=(400, 40)):
                await app.workers.wait_for_complete()
                card = SimpleNamespace(endpoint={'device': 'metrics-alias', 'url': 'http://100.64.0.2:8003/v1/system'},
                    connected=True, cpu=.12, reading={'sampled_at': time.monotonic(), 'chip': 'CPU',
                    'memory': {'used': 32, 'total': 64},
                    'gpus': [{'name': 'NVIDIA GeForce Example GPU', 'used': 18, 'total': 24}]})
                app.remote_cards = [card]
                app.show_inference(state)
                table = app.query_one('#inference', DataTable)
                row = table.get_row('Chat:qwen')
                self.assertEqual(str(row[InferenceColumn.DEVICE_INFO]).strip(), 'NVIDIA Example GPU')
                self.assertIn('CPU 12% · RAM 50% · VRAM 75%', app.row_details['inference']['Chat:qwen'])
                self.assertEqual([str(cell).strip() for cell in table.get_row('Chat:deepseek')[1:3]], [''] * 2)
                table.move_cursor(row=table.get_row_index('Chat:qwen'))
                before = table.cursor_row
                card.reading['sampled_at'] -= 13
                app.show_inference(state)
                self.assertIn('CPU — · RAM — · VRAM —', app.row_details['inference']['Chat:qwen'])
                self.assertEqual(table.cursor_row, before)
                card.reading['sampled_at'] = time.monotonic()
                card.connected = False
                app.show_inference(state)
                self.assertIn('CPU — · RAM — · VRAM —', app.row_details['inference']['Chat:qwen'])

    async def asyncSetUp(self):
        startup = patch.object(DotAgents, 'start_inference')
        startup.start()
        self.addCleanup(startup.stop)
        logs = patch("tui.ServiceLogs.sync")
        logs.start()
        self.addCleanup(logs.stop)
        inference_timer = patch.object(DotAgents, 'refresh_inference')
        inference_timer.start()
        self.addCleanup(inference_timer.stop)
        peers = patch('remote_telemetry.load_endpoints', return_value=[])
        peers.start()
        self.addCleanup(peers.stop)
        # These layout tests must never probe the host through the periodic timer.
        timer = patch.object(DotAgents, 'refresh_hardware')
        timer.start()
        self.addCleanup(timer.stop)

    async def test_stalled_sources_do_not_block_local_or_other_harness_updates(self):
        state = observation(loaded=('qwen-omlx',))
        state.update(inference_sampled_at=1, inference={})
        remote_started, chat_started, release = threading.Event(), threading.Event(), threading.Event()
        calls = []
        def remote():
            calls.append('remote')
            remote_started.set()
            if not release.wait(5):
                raise TimeoutError('Test did not release remote')
            return []
        def chat():
            chat_started.set()
            if not release.wait(5):
                raise TimeoutError('Test did not release chat')
            return dict(ready=True, active=0, sampled_at=2)
        with patch.object(control_plane, 'snapshot', return_value=state):
            app = DotAgents()
            async with app.run_test(size=(120, 40)):
                await app.workers.wait_for_complete()
                app.poll_source('remote:slow', remote)
                app.poll_source('harness:chat', chat)
                try:
                    self.assertTrue(await asyncio.to_thread(remote_started.wait, 2))
                    self.assertTrue(await asyncio.to_thread(chat_started.wait, 2))
                    await app.poll_source('remote:slow', remote).wait()
                    self.assertEqual(calls, ['remote'])
                    local = dict(inference={'state': 'ready'}, models=state['models'],
                                 inference_services=state['inference_services'], inference_sampled_at=10)
                    await app.poll_source('local', lambda: local).wait()
                    await app.poll_source('harness:agent', lambda: dict(ready=True, active=1, sampled_at=10)).wait()
                    self.assertEqual(app.last_snapshot['inference_sampled_at'], 10)
                    self.assertTrue(app.last_snapshot['harness']['activity']['coordinator'])
                    self.assertIn('remote:slow', app.polling)
                    self.assertIn('harness:chat', app.polling)
                finally:
                    release.set()
                    await app.workers.wait_for_complete()
                self.assertEqual(app.last_snapshot['harness']['services']['chat']['sampled_at'], 2)
                self.assertEqual(app.last_snapshot['harness']['services']['agent']['sampled_at'], 10)
                self.assertEqual(app.last_snapshot['inference_sampled_at'], 10)
                self.assertFalse(app.polling)

    async def test_local_updates_do_not_refresh_remote_chart_timestamp(self):
        state = observation(loaded=('qwen-omlx',))
        now = time.monotonic()
        remote = dict(id='remote-chat', name='Chat', role='service', state='ready',
                      node={'label': 'test', 'location': 'remote'},
                      endpoints=['http://100.64.0.2:8001/v1'], model_ids=['qwen'],
                      model_states={'qwen': 'loaded'}, model_activity={'qwen': 'running'},
                      engine='Example', execution='docker', detail='Remote node',
                      sampled_at=now - 20, observation_source='remote:test')
        state.update(inference_sampled_at=now, inference={})
        with patch.object(control_plane, 'snapshot', return_value=state):
            app = DotAgents()
            async with app.run_test(size=(160, 40)):
                await app.workers.wait_for_complete()
                card = RemoteHardware({'device': 'test', 'url': 'http://100.64.0.2:8003/v1/system'})
                await app.query_one('#hardware-cards').mount(card)
                app.remote_observations['remote:test'] = [remote]
                app.show_snapshot(state)
                self.assertFalse(card.query_one(InferenceChart).running)
                state['inference_sampled_at'] = time.monotonic()
                app.show_snapshot(state)
                self.assertFalse(card.query_one(InferenceChart).running)
                rows = [row for row in app.last_snapshot['inference_services'] if row.get('id') == 'remote-chat']
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]['sampled_at'], now - 20)

    def test_chart_history_does_not_invent_samples_or_zero_for_failure(self):
        chart = UsageChart('RAM', '#4ade80')
        chart.sample(1, .5, '4/8 GiB')
        chart.sample(1, .5, '4/8 GiB')
        self.assertEqual(len(chart.samples), 1)
        chart.sample(2, None, 'Unavailable')
        self.assertIsNone(chart.samples[-1][1])

    async def test_older_stack_observation_cannot_overwrite_newer_inference_activity(self):
        state = observation()
        state.update(inference_sampled_at=20, inference={})
        with patch.object(control_plane, 'snapshot', return_value=state):
            app = DotAgents()
            async with app.run_test(size=(120, 40)):
                await app.workers.wait_for_complete()
                old = observation()
                old.update(inference_sampled_at=10, inference={'stale': True})
                app.show_snapshot(old)
                self.assertEqual(app.last_snapshot['inference_sampled_at'], 20)
                self.assertNotIn('stale', app.last_snapshot['inference'])

    def test_chart_leaves_missing_samples_as_gaps_in_the_plot(self):
        chart = UsageChart('CPU', '#60a5fa')
        chart.sample(0, .2, '')
        chart.sample(3, None, '')
        chart.sample(6, .8, '')
        with patch.object(UsageChart, 'size', new_callable=PropertyMock, return_value=Size(100, 10)):
            plot = chart.render().plain
        self.assertEqual(sum(0x2801 <= ord(char) <= 0x28ff for char in plot), 2)

    def test_inference_chart_has_independent_scales_and_explicit_failure_gaps(self):
        history = SpeedHistory()
        history.sample('prefill', 94, 1000)
        history.sample('prefill', 95, None)
        history.sample('decode', 96, 40)
        history.sample('decode', 97, None)
        chart = InferenceChart('local')
        chart.show_history(history, 100, 'Loaded model')
        with patch.object(InferenceChart, 'size', new_callable=PropertyMock, return_value=Size(60, 9)):
            rendered = chart.render().plain
            self.assertIn('1000│', rendered)
            self.assertIn(' 40│', rendered)
            self.assertGreaterEqual(sum(0x2801 <= ord(char) <= 0x28ff for char in rendered), 2)
            chart.show_history(history, 200, 'Loaded model')
            self.assertFalse(any(0x2801 <= ord(char) <= 0x28ff for char in chart.render().plain))
        with patch.object(InferenceChart, 'size', new_callable=PropertyMock, return_value=Size(26, 3)):
            self.assertEqual(len(chart.render().plain.splitlines()), 3)
            self.assertIn('Decode', chart.render().plain)
        self.assertEqual(len(history.samples['prefill']), 2)

    def test_chat_charts_show_integer_rates_and_a_zero_baseline_at_compact_sizes(self):
        history = SpeedHistory()
        history.sample('prefill', 100, 1062.4)
        history.sample('decode', 100, 56.7)
        chart = InferenceChart('local')
        chart.show_history(history, 100, 'Loaded model')
        for height in (3, 9):
            with self.subTest(height=height), patch.object(
                    InferenceChart, 'size', new_callable=PropertyMock, return_value=Size(100, height)):
                rendered = chart.render().plain
                self.assertIn('Prefill 1062 tok/s', rendered)
                self.assertIn('Decode 57 tok/s', rendered)
                self.assertNotIn('Last', rendered)
                self.assertIn('−60s', rendered)
                self.assertNotIn('1062.4', rendered)
                self.assertNotIn('e+', rendered)
                self.assertIn('1063│', rendered)
                self.assertIn('57│', rendered)
                self.assertGreaterEqual(rendered.count('  0│'), 2)

    def test_speed_charts_share_a_row_and_keep_each_axis_fixed(self):
        for width in (26, 60, 100):
            for height in (3, 6, 9):
                with self.subTest(width=width, height=height), patch.object(
                        InferenceChart, 'size', new_callable=PropertyMock, return_value=Size(width, height)):
                    legend_width = label_space(width)
                    left_width = (width - legend_width - 3) // 2
                    axes = [legend_width + 8, legend_width + left_width + 1,
                            legend_width + left_width + 3 + 8]
                    chart = InferenceChart('local')
                    for value in (None, 0, 40, 1000, 99999, 1000000, 0):
                        history = SpeedHistory()
                        if value is not None:
                            history.sample('prefill', 100, value)
                            history.sample('decode', 100, 40)
                        chart.show_history(history, 100, 'Loaded model')
                        lines = chart.render().plain.splitlines()
                        self.assertEqual(len(lines), height)
                        self.assertTrue(lines[0].startswith('Prefill'))
                        self.assertTrue(lines[1].startswith('Decode'))
                        if left_width >= 10:
                            self.assertTrue(all([i for i, char in enumerate(line) if char == '│'] == axes
                                                for line in lines[:-1]))
                            self.assertNotIn('Prefill', lines[0][legend_width:])
                            self.assertNotIn('Decode', lines[1][legend_width:])
                            self.assertEqual(lines[-1].count('−60s'), 2)
                        self.assertTrue(all(len(line) == width for line in lines))

    def test_speed_chart_colors_follow_activity_and_device_info_uses_one_order(self):
        history = SpeedHistory()
        history.sample('prefill', 100, 1000)
        history.sample('decode', 100, 40)
        chart = InferenceChart('local')
        with patch.object(InferenceChart, 'size', new_callable=PropertyMock, return_value=Size(80, 6)):
            for running in (True, False):
                chart.show_history(history, 100, 'Model', running=running)
                rendered = chart.render()
                for label, color in [('Prefill', '#60a5fa'), ('Decode', '#4ade80')]:
                    style = rendered.get_style_at_offset(Console(), rendered.plain.index(label))
                    self.assertEqual(style.color.get_truecolor(),
                                     Color.parse(color if running else '#94a3b8').get_truecolor())
        self.assertEqual(hardware_label(dict(chip='CPU', cores=32, gpus=[dict(name='NVIDIA GeForce Example GPU',
                         power_watts=75.5, power_limit_watts=350)])), 'Example GPU · 32 CPUs · Power 76/350 W')

    async def test_mac_charts_pair_ram_and_gpu_with_separate_gib_readings(self):
        hardware = dict(sampled_at=100, chip='Apple M3 Ultra', os='macOS', cores=32,
                        cpu_ticks=None, memory=dict(used=80 * 2**30, total=256 * 2**30, shared=True),
                        gpus=[dict(id='apple', name='Apple GPU', used=40 * 2**30,
                                   total=256 * 2**30, shared=True, utilization=20)])
        for size in ((80, 24), (120, 40)):
            with self.subTest(size=size), patch.object(control_plane, 'snapshot', return_value=observation()), \
                    patch.object(DotAgents, 'start_inference'), patch('tui.ServiceLogs.sync'), \
                    patch('remote_telemetry.load_endpoints', return_value=[]):
                app = DotAgents()
                async with app.run_test(size=size) as pilot:
                    await app.workers.wait_for_complete()
                    app.show_hardware(hardware)
                    await pilot.pause()
                    speeds = app.query_one('#inference-chart')
                    self.assertIn('Host · ', str(app.query_one('#local-title').render()))
                    self.assertEqual(hardware_label(hardware), 'Apple M3 Ultra · 32 CPUs')
                    ram = app.query_one('#ram-chart', UsageChart)
                    gpu = app.query_one('#gpu-chart', UsageChart)
                    self.assertFalse(list(app.query('#disk-chart')))
                    self.assertLessEqual(speeds.region.bottom, ram.region.y)
                    self.assertEqual(ram.region.y, gpu.region.y)
                    self.assertLessEqual(ram.region.right, gpu.region.x)
                    legend = ram.parent.query_one(MemoryLegend)
                    self.assertEqual(legend.region.x, speeds.region.x)
                    self.assertEqual(legend.render().plain.splitlines(),
                                     ['RAM 80.0/256 GiB', 'GPU 40.0/256 GiB'])
                    speed_line = speeds.render().plain.splitlines()[0]
                    for chart, axis in zip((ram, gpu), (speed_line.index('│'), speed_line.rindex('│'))):
                        chart_axis = chart.content_region.x + chart.render().plain.splitlines()[0].index('│')
                        self.assertEqual(chart_axis, speeds.content_region.x + axis)
                    self.assertFalse(ram.extra)
                    self.assertAlmostEqual(gpu.current_value(), 40 / 256)
                    self.assertIn('shared RAM', gpu.tooltip.plain)
                    self.assertTrue(app.query_one('#system-panel').region.contains_region(gpu.region))
                    self.assertIn('1s samples', str(app.query_one('#system-panel').border_subtitle))

    async def test_all_panels_and_controls_stay_visible_without_tabs(self):
        for size in ((80, 24), (120, 40)):
            app = DotAgents()
            with self.subTest(size=size), \
                    patch.object(control_plane, 'snapshot', side_effect=lambda root, **kwargs: observation()), \
                    patch('jobs.asyncio.create_subprocess_exec') as spawn:
                async with app.run_test(size=size) as pilot:
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                    self.assertFalse(list(app.query(TabbedContent)))
                    self.assertFalse(list(app.query(Tabs)))
                    self.assertEqual(len(app.query(Button)), 0)
                    self.assertEqual(len(app.query(Select)), 0)
                    self.assertFalse(list(app.query('#detail')))
                    self.assertFalse(list(app.query('#selection')))
                    self.assertFalse(list(app.query('#overview')))
                    self.assertEqual(app.query_one(Header).region.bottom,
                                     app.query_one('#dashboard').region.y)
                    self.assertEqual(app.query_one('#dashboard').region.bottom,
                                     app.query_one(Footer).region.y)
                    self.assertFalse(list(app.query('#models-panel')))
                    self.assertFalse(list(app.query('#devices-panel')))
                    for key in ('qwen-omlx', 'deepseek'):
                        self.assertIn(key, app.row_details['inference'])
                    charts = list(app.query(UsageChart))
                    self.assertEqual(len(charts), 2)
                    for chart in charts:
                        self.assertGreaterEqual(chart.size.height, 3)
                        if size[1] >= 40:
                            self.assertTrue(app.query_one('#system-panel').region.contains_region(chart.region))
                        self.assertIn('100│', chart.render().plain)
                        self.assertIn('  0│', chart.render().plain)
                        legend = chart.parent.query_one(MemoryLegend)
                        self.assertIn(chart.label, legend.render().plain)
                        self.assertLessEqual(legend.region.right, chart.region.x)
                    self.assertEqual(charts[0].region.y, charts[1].region.y)
                    speeds = app.query_one(InferenceChart)
                    self.assertLessEqual(speeds.region.bottom, charts[0].region.y)
                    self.assertIn('Prefill', speeds.render().plain)
                    self.assertIn('Decode', speeds.render().plain)
                    self.assertLessEqual(charts[0].region.right, charts[1].region.x)
                    system = app.query_one('#system-panel').region
                    stack = app.query_one('#stack-panel').region
                    activity = app.query_one('#activity-panel').region
                    inference = app.query_one('#inference-panel').region
                    self.assertLessEqual(system.width, size[0])
                    self.assertEqual(inference.width, stack.width)
                    self.assertEqual(inference.width, system.width)
                    self.assertFalse(list(app.query('#inference-speed-panel')))
                    self.assertFalse(list(app.query('#speed-grid')))
                    self.assertLessEqual(system.bottom, stack.y)
                    self.assertLessEqual(stack.bottom, inference.y)
                    self.assertEqual(stack.x, inference.x)
                    self.assertLessEqual(inference.bottom, activity.y)
                    self.assertEqual(activity.x, system.x)
                    self.assertEqual(activity.width, system.width)
                    self.assertTrue(app.native_ansi_color)
                    self.assertEqual(app.screen.styles.background.ansi, -1)
                    for selector in ('Header', 'Footer', 'DataTable', 'RichLog', 'Input'):
                        self.assertEqual(app.query_one(selector).styles.background.ansi, -1)
                    left = app.query_one('#stack', DataTable)
                    right = app.query_one('#docker-stack', DataTable)
                    self.assertEqual([str(left.get_row_at(i)[0]) for i in range(left.row_count)],
                                     ['Tunnel', 'Gateway', 'Authentication', 'Chat', 'Agent', 'Image workspace', 'Model APIs'])
                    self.assertEqual(right.row_count, 2)
                    self.assertIn('3000', str(app.query_one('#stack', DataTable).get_row('Chat')[1]))
                    self.assertEqual(left.region.y, right.region.y)
                    self.assertLessEqual(left.region.right, right.region.x)
                    for table in (left, right):
                        self.assertTrue(app.query_one('#stack-panel').region.contains_region(table.region))
                    self.assertLessEqual(left.virtual_size.width, left.size.width)
                    inference_table = app.query_one('#inference', DataTable)
                    for model in (m for m in observation()['models'] if m.get('endpoint')):
                        self.assertEqual(str(inference_table.get_row(model['id'])[InferenceColumn.MODEL]).strip(),
                                         '✦ ' + model['name'])
                    self.assertGreaterEqual(inference_table.ordered_columns[InferenceColumn.ENGINE].width, len('Engine'))
                    self.assertIn('Engine', str(inference_table.ordered_columns[InferenceColumn.ENGINE].label))
                    self.assertFalse(any('Steps/s' in str(column.label) for column in inference_table.ordered_columns))
                    labels = {str(column.label).strip() for column in inference_table.ordered_columns}
                    self.assertFalse(labels & {'CPU', 'RAM', 'VRAM', 'Prefill tok/s', 'Decode tok/s', 'Time s', 'IP:port'})
                    for row in inference_table.ordered_rows:
                        connection = str(inference_table.get_row(row.key)[InferenceColumn.CONNECTION]).strip()
                        self.assertIn(connection, ('', '●'))
                    for index in (InferenceColumn.CONNECTION, InferenceColumn.DEVICE, InferenceColumn.CAPABILITY,
                                  InferenceColumn.MODEL, InferenceColumn.ENGINE, InferenceColumn.STATE):
                        column = inference_table.ordered_columns[index]
                        self.assertGreaterEqual(column.width, column.label.cell_len)
                        for row in inference_table.ordered_rows:
                            cell = inference_table.get_row(row.key)[index]
                            if isinstance(cell, Status):
                                cell = cell.render()
                            self.assertGreaterEqual(column.width, getattr(cell, 'cell_len', len(str(cell))))
                    self.assertTrue(all(row.height == 1 for row in
                                        app.query_one('#inference', DataTable).ordered_rows))
                    for widget_id in ('system-panel', 'stack-panel', 'inference-panel', 'activity-panel'):
                        widget = app.query_one('#' + widget_id)
                        self.assertTrue(widget.visible, widget_id)
                        self.assertGreater(widget.region.height, 0, widget_id)
                        if size[1] >= 40:
                            self.assertTrue(app.screen.region.contains_region(widget.region),
                                            f'{widget_id}: {widget.region} outside {app.screen.region}')
                    if size[1] < 40:
                        app.query_one('#activity-panel').scroll_visible(animate=False)
                        await pilot.pause()
                        self.assertTrue(app.screen.region.contains_region(app.query_one('#activity-panel').region))
                    self.assertEqual(app.query_one('#inference-panel').border_subtitle, 'Model APIs: 0/1 connected')
                    spawn.assert_not_called()

    async def test_refresh_keeps_model_selection_and_updates_service_state(self):
        state = observation(loaded=('qwen-omlx', 'deepseek'))
        app = DotAgents()
        with patch.object(control_plane, 'snapshot', side_effect=lambda root, **kwargs: state):
            async with app.run_test(size=(120, 40)) as pilot:
                await app.workers.wait_for_complete()
                table = app.query_one('#inference', DataTable)
                table.move_cursor(row=table.get_row_index('deepseek'))
                selected = table.cursor_row
                state['inference_services'][0]['state'] = 'running'
                app.action_refresh()
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertEqual(table.cursor_row, selected)
                self.assertEqual(app.query_one('#inference-panel').border_subtitle, 'Model APIs: 1/1 connected')
                await pilot.resize_terminal(80, 24)
                await pilot.pause()
                self.assertEqual(table.cursor_row, selected)
                self.assertGreater(table.size.width, 0)

    async def test_remote_service_failure_does_not_change_local_api_status(self):
        state = observation()
        service = state['inference_services'][0]
        service.update(state='unknown', node={'label': 'mac-b', 'location': 'remote'},
                       endpoints=['https://mac-b.example.test/v1'])
        with patch.object(control_plane, 'snapshot', return_value=state):
            app = DotAgents()
            async with app.run_test(size=(120, 40)):
                await app.workers.wait_for_complete()
                table = app.query_one('#inference', DataTable)
                self.assertNotIn('Main API', app.row_details['inference'])
                gateway = app.query_one('#docker-stack', DataTable).get_row('caddy')
                self.assertEqual(str(gateway[0]), 'dotagents-caddy-1')
                self.assertIn('healthy', str(gateway[1]))
                self.assertGreater(table.row_count, 0)
                self.assertEqual(app.query_one('#inference-panel').border_title, 'Inference')

    async def test_discovered_nodes_remain_visible_without_speed_graphs(self):
        state = observation()
        state['devices'] = control_plane.device_rows({'Peer': {
            'a': {'HostName': 'a', 'Online': True, 'Active': True, 'CurAddr': '192.0.2.1:42'},
            'b': {'HostName': 'b', 'Online': True},
            'c': {'HostName': 'c', 'Online': False}}})
        with patch.object(control_plane, 'snapshot', return_value=state):
            app = DotAgents()
            async with app.run_test(size=(180, 40)):
                await app.workers.wait_for_complete()
                table = app.query_one('#inference', DataTable)
                self.assertGreater(table.row_count, 0)
                for key in ('peer:a', 'peer:b', 'peer:c'):
                    self.assertIn(key, app.row_details['inference'])
                    self.assertNotIn(key, app.speed_histories)
                self.assertEqual(app.query_one('#stack-panel').border_title, 'Stack')

    async def test_only_quit_shortcut_remains(self):
        app = DotAgents()
        binding, = app.BINDINGS
        self.assertEqual((binding.key, binding.action, binding.description), ('q', 'quit', 'Quit'))
        self.assertTrue(binding.priority)
        with patch.object(control_plane, 'snapshot', return_value=observation()), \
                patch('jobs.asyncio.create_subprocess_exec') as spawn:
            async with app.run_test(size=(120, 45)) as pilot:
                await app.workers.wait_for_complete()
                await pilot.press('b', 'd', 'c', 'v', 's', 'r')
                spawn.assert_not_called()
                with patch.object(app.service_logs, 'close', new_callable=AsyncMock) as close:
                    await pilot.press('q')
                    close.assert_awaited()
            app.flush_logs()
            app.animate_status()
            app.refresh_speed_rows()

    async def test_activity_shows_source_and_plain_log_text_with_bounded_queue(self):
        with patch.object(control_plane, 'snapshot', return_value=observation()):
            app = DotAgents()
            async with app.run_test(size=(120, 40)):
                await app.workers.wait_for_complete()
                app.log_queue.clear()
                app.receive_log('caddy', '\x1b[31mrequest complete\x1b[0m [bold]literal')
                with patch.object(app.query_one('#output'), 'write') as write:
                    app.flush_logs()
                console = Console(width=80, color_system=None)
                with console.capture() as captured:
                    console.print(write.call_args.args[0])
                rendered = captured.get()
                self.assertIn('Caddy', rendered)
                self.assertIn('INFO', rendered)
                self.assertIn('request complete', rendered)
                self.assertIn('[bold]literal', rendered)
                for i in range(2100):
                    app.receive_log('worker', str(i))
                self.assertEqual(len(app.log_queue), 2000)
                self.assertEqual(app.dropped_logs, 100)

    def test_log_text_cannot_emit_terminal_controls_or_rich_markup(self):
        value = '\x1b[31mred\x1b[0m [bold]plain[/bold]\x00'
        self.assertEqual(plain_output(value), 'red [bold]plain[/bold]')

    async def test_status_animation_keeps_selection_and_respects_container_health(self):
        state = observation()
        state['containers'] = [
            {'name': 'checked', 'state': 'running', 'health': 'healthy'},
            {'name': 'failed', 'state': 'running', 'health': 'unhealthy'},
            {'name': 'worker', 'state': 'running', 'health': '—'},
        ]
        with patch.object(control_plane, 'snapshot', return_value=state):
            app = DotAgents()
            async with app.run_test(size=(120, 40)):
                await app.workers.wait_for_complete()
                table = app.query_one('#docker-stack', DataTable)
                table.move_cursor(row=table.get_row_index('compose:worker'))
                cursor = table.cursor_row
                healthy = table.get_row('compose:checked')[1]
                failed = table.get_row('compose:failed')[1]
                self.assertEqual(healthy.plain, '● healthy')
                self.assertEqual(healthy.get_style_at_offset(app.console, 0).color.triplet, (74, 222, 128))
                self.assertEqual(failed.plain, '● unhealthy')
                self.assertEqual(failed.get_style_at_offset(app.console, 0).color.triplet, (250, 204, 21))
                before = table.get_row('compose:worker')[1].plain
                app.animate_status()
                after = table.get_row('compose:worker')[1]
                self.assertEqual(before, after.plain)
                self.assertEqual(after.plain, '● healthy')
                self.assertEqual(after.get_style_at_offset(app.console, 0).color.triplet, (74, 222, 128))
                self.assertEqual(table.cursor_row, cursor)
                state['containers'][2]['state'] = 'exited'
                app.show_snapshot(state)
                app.animate_status()
                self.assertEqual(table.get_row('compose:worker')[1].plain, '● disabled')
                self.assertEqual(app.animated_cells['docker-stack'], [])

    def test_configuration_and_planned_states_do_not_claim_health(self):
        for state in ('configured', 'unverified', 'planned', 'Present', 'unknown'):
            rendered = Status(state).render()
            self.assertEqual(rendered.style, '#94a3b8')
            self.assertFalse(Status(state).animated)

    async def test_application_details_stay_in_table_without_replacing_logs(self):
        with patch.object(control_plane, 'snapshot', side_effect=lambda root, **kwargs: observation(loaded=('deepseek',))):
            app = DotAgents()
            async with app.run_test(size=(120, 40)) as pilot:
                await app.workers.wait_for_complete()
                table = app.query_one('#docker-stack', DataTable)
                table.focus()
                table.move_cursor(row=table.get_row_index('chat'))
                with patch.object(app.query_one('#output'), 'write') as write:
                    await pilot.press('enter')
                write.assert_not_called()
                self.assertIn('Chat Harness owns sessions, attachments, settings', app.row_details['docker-stack']['chat'])
                self.assertEqual(app.focused, table)
                self.assertNotIn('harness', app.row_details['docker-stack'])
                self.assertNotIn('main-api', app.row_details['docker-stack'])
                inference = app.query_one('#inference', DataTable)
                self.assertEqual(str(inference.get_row('deepseek')[InferenceColumn.ENGINE]).strip(), 'DS4')
                self.assertIn('ds4', app.row_details['inference']['deepseek'])
                self.assertIn('native', app.row_details['inference']['deepseek'])
                self.assertNotIn('qwen-image21', app.row_details['inference'])

    async def test_numeric_speeds_keep_endpoint_histories_separate(self):
        state = observation()
        remote = dict(id='node-a', name='Chat', role='service', state='ready',
                      node={'label': 'node-a', 'location': 'remote'},
                      endpoints=['http://100.64.0.2:8001/v1'], model_ids=['qwen'],
                      model_states={'qwen': 'loaded'},
                      model_metrics={'qwen': {'prefill_tps': 250, 'decode_tps': 40, 'age_seconds': 2}},
                      engine='Example', execution='docker', detail='Remote node')
        other = dict(remote, id='node-b', node={'label': 'node-b', 'location': 'remote'},
                     model_metrics={'qwen': {'prefill_tps': 500, 'decode_tps': 80, 'age_seconds': 2}})
        state['inference_services'].extend([remote, other])
        state['inference_sampled_at'] = time.monotonic()
        with patch.object(control_plane, 'snapshot', return_value=state):
            app = DotAgents()
            async with app.run_test(size=(160, 40)) as pilot:
                await app.workers.wait_for_complete()
                await pilot.pause()
                table = app.query_one('#inference', DataTable)
                a, b = (app.speed_histories[key] for key in ('node-a:qwen', 'node-b:qwen'))
                self.assertIsNot(a, b)
                for key, prefill, decode in [('node-a:qwen', '250.0', '40.0'),
                                              ('node-b:qwen', '500.0', '80.0')]:
                    self.assertAlmostEqual(app.speed_histories[key].value('prefill', time.monotonic()), float(prefill))
                    self.assertAlmostEqual(app.speed_histories[key].value('decode', time.monotonic()), float(decode))
                    self.assertEqual(table.get_row_height(key), 1)
                cursor = table.get_row_index('node-a:qwen')
                table.move_cursor(row=cursor)
                app.refresh_speed_rows()
                self.assertEqual(table.cursor_row, cursor)
                state['inference_services'].pop()
                app.show_inference(state)
                self.assertNotIn('node-b:qwen', app.speed_histories)

    async def test_remote_speed_chart_matches_endpoint_when_device_alias_differs(self):
        state = observation()
        state['inference_services'].append(dict(
            id='remote-chat', name='Chat', role='service', state='ready',
            node={'label': 'gpu-box', 'location': 'remote'},
            endpoints=['http://100.64.0.2:8001/v1'], model_ids=['qwen'],
            model_states={'qwen': 'loaded'},
            model_metrics={'qwen': {'decode_tps': 40, 'age_seconds': 2}},
            engine='Example', execution='docker', detail='Remote node'))
        with patch.object(control_plane, 'snapshot', return_value=state):
            app = DotAgents()
            async with app.run_test(size=(160, 40)):
                await app.workers.wait_for_complete()
                card = RemoteHardware({'device': 'metrics-alias', 'url': 'http://100.64.0.2:8003/v1/system'})
                await app.query_one('#hardware-cards').mount(card)
                app.show_inference(state)
                chart = card.query_one(InferenceChart)
                self.assertIs(chart.history, app.speed_histories['remote-chat:qwen'])
                self.assertIsNone(app.query_one('#inference-chart', InferenceChart).history)
                card.show_reading({'state': 'offline', 'sampled_at': time.monotonic()})
                self.assertIs(chart.history, app.speed_histories['remote-chat:qwen'])
                state['inference_services'][-1]['model_states']['qwen'] = 'unknown'
                app.show_inference(state)
                self.assertIsNone(chart.history)

    async def test_system_speeds_follow_loaded_model_switches_without_hiding_nodes(self):
        state = observation(loaded=('deepseek',))
        state['devices'] = [dict(name='peer', address='100.64.0.3', connection='Offline', trust='Discovered')]
        with patch.object(control_plane, 'snapshot', return_value=state):
            app = DotAgents()
            async with app.run_test(size=(120, 40)) as pilot:
                await app.workers.wait_for_complete()
                await pilot.pause()
                table = app.query_one('#inference', DataTable)
                self.assertIsNone(app.speed_histories['deepseek'].value('decode', time.monotonic()))
                self.assertNotIn('Last measured:', app.row_details['inference']['qwen-omlx'])
                history = app.speed_histories['deepseek']
                history.sample('decode', time.monotonic() - 2, 40)
                qwen = next(model for model in state['models'] if model['id'] == 'deepseek')
                qwen['activity'] = 'running'
                app.show_inference(state)
                self.assertIs(app.speed_histories['deepseek'], history)
                chart = app.query_one(InferenceChart)
                self.assertIs(chart.history, history)
                self.assertTrue(chart.running)
                self.assertIn('DeepSeek V4 Flash 0731', chart.tooltip)
                self.assertIn('Decode tok/s: 40.0', app.row_details['inference']['deepseek'])
                qwen['activity'] = 'loaded'
                app.show_inference(state)
                self.assertFalse(chart.running)
                self.assertIs(chart.history, history)
                self.assertEqual(history.chart_samples('decode', time.monotonic() + 180)[-1][1], 40)
                self.assertIn('Decode tok/s: 40.0', app.row_details['inference']['deepseek'])
                qwen.update(runtime='standby', activity='unloaded')
                deepseek = next(model for model in state['models'] if model['id'] == 'qwen-omlx')
                deepseek.update(runtime='loaded', activity='loaded')
                app.show_inference(state)
                self.assertNotIn('Last measured:', app.row_details['inference']['deepseek'])
                self.assertIsNone(app.speed_histories['qwen-omlx'].value('decode', time.monotonic()))
                self.assertIs(chart.history, app.speed_histories['qwen-omlx'])
                deepseek.update(runtime='unknown', activity='unknown')
                app.show_inference(state)
                self.assertNotIn('Last measured:', app.row_details['inference']['qwen-omlx'])
                self.assertIsNone(chart.history)
                self.assertIn('peer:peer', app.row_details['inference'])
                self.assertNotIn('peer:peer', app.speed_histories)


if __name__ == '__main__':
    unittest.main()
