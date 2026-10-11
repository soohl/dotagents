"""Protect the optional worker's credentials, routing, and model ownership."""
from http.server import ThreadingHTTPServer
import io
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
import unittest
from unittest.mock import MagicMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import launcher
import omlx_backend
import inference_gateway as gateway
from inference_speeds import SpeedHistory
from test_inference_gateway import FakeBackend, FakeManager


class OmlxBackendTests(unittest.TestCase):
    def test_engine_validation_bounds_both_subprocess_probes(self):
        for stage in ('version', 'native kernels'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'omlx').touch()
                config = dict(executable='omlx', python='python', version='0.7.0')
                responses = [subprocess.TimeoutExpired('omlx', 15)] if stage == 'version' else [
                    '0.7.0', subprocess.TimeoutExpired('python', 15)]
                with patch.object(launcher, 'ROOT', root), \
                        patch.object(launcher, 'check_revision'), \
                        patch.object(omlx_backend, 'settings', return_value=config), \
                        patch.object(omlx_backend.platform, 'system', return_value='Darwin'), \
                        patch.object(omlx_backend.platform, 'machine', return_value='arm64'), \
                        patch.object(omlx_backend.subprocess, 'check_output', side_effect=responses) as run:
                    with self.assertRaisesRegex(ValueError, stage + ' check timed out'):
                        omlx_backend.validate(dict(source='source', revision='pin'), weights=False)
                    self.assertTrue(all(call.kwargs['timeout'] == 15 for call in run.call_args_list))

    def test_gateway_records_stream_and_regular_usage_in_only_the_omlx_model_log(self):
        usage = dict(prompt_tokens=100, completion_tokens=50,
                     prompt_eval_duration=.5, generation_duration=2,
                     prompt_tokens_per_second=200, generation_tokens_per_second=25)
        token = b'data: {"choices":[{"delta":{"content":"private response"}}]}\n\n'
        usage_event = ('data: ' + json.dumps({'choices': [], 'usage': usage}) + '\n\n').encode()
        done = b'data: [DONE]\n\n'
        requests = []
        class Backend(FakeBackend):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append(payload)
                streaming = payload.get('stream') is True
                response = (token + usage_event + done if streaming else
                            json.dumps({'choices': [{'message': {'content': 'private response'}}],
                                        'usage': usage}).encode())
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream' if streaming else 'application/json')
                self.end_headers()
                self.wfile.write(response)
        with tempfile.TemporaryDirectory() as directory:
            manager = FakeManager(Path(directory))
            manager.state = Path(directory)
            manager.models = launcher.profiles()['models']
            manager.record_usage = lambda key, metrics: gateway.ModelGateway.record_usage(manager, key, metrics)
            backend = ThreadingHTTPServer(('127.0.0.1', 0), Backend)
            frontend = ThreadingHTTPServer(('127.0.0.1', 0), gateway.make_handler(manager))
            servers = [backend, frontend]
            for server in servers:
                threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                with patch.object(gateway, 'BACKEND_PORT', backend.server_port):
                    for streaming, include in [(False, False), (True, False), (True, True)]:
                        payload = dict(model=manager.models['qwen-omlx']['api_model'],
                                       messages=[{'role': 'user', 'content': 'private prompt'}],
                                       stream=streaming, stream_options={'include_usage': include})
                        request = urllib.request.Request(
                            f'http://127.0.0.1:{frontend.server_port}/v1/chat/completions',
                            data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
                        with urllib.request.urlopen(request) as response:
                            body = response.read()
                        if streaming:
                            self.assertEqual(body, token + (usage_event if include else b'') + done)
                            self.assertTrue(requests[-1]['stream_options']['include_usage'])
                        else:
                            self.assertEqual(json.loads(body)['usage'], usage)
                log = manager.state / 'qwen-omlx.log'
                history = SpeedHistory()
                for line in log.read_text().splitlines():
                    history.feed_log(line)
                self.assertEqual(history.latest['prefill_tps'], 200)
                self.assertEqual(history.latest['decode_tps'], 25)
                self.assertNotIn('private', log.read_text())
                self.assertFalse((manager.state / 'qwen.log').exists())
                self.assertEqual(log.stat().st_mode & 0o777, 0o600)
            finally:
                for server in servers:
                    server.shutdown()
                    server.server_close()

    def test_runtime_uses_only_project_model_and_cache_paths(self):
        model = launcher.profiles()['models']['qwen-omlx']
        config = omlx_backend.settings()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'config').mkdir()
            (root / 'config/omlx.json').write_text(json.dumps(config))
            (root / '.env').write_text('OMLX_API_KEY=test-worker-key\n')
            environment = {'LLM_PORT': '8100', 'OMLX_API_KEY': 'inherited-stale-key'}
            with patch.object(launcher, 'ROOT', root), patch.object(omlx_backend, 'validate'):
                args = omlx_backend.server_args(model, environment)
            self.assertEqual(args[args.index('--host') + 1], '127.0.0.1')
            self.assertEqual(args[args.index('--port') + 1], '8100')
            self.assertIn('--no-hf-cache', args)
            self.assertNotIn('test-worker-key', ' '.join(args))
            self.assertNotIn('OMLX_API_KEY', environment)
            self.assertEqual(omlx_backend.headers(), {})
            settings_path = root / config['base_path'] / 'model_settings.json'
            runtime = json.loads((settings_path.parent / 'settings.json').read_text())
            self.assertFalse(runtime['usage']['usage_history'])
            self.assertTrue(runtime['auth']['allow_unauthenticated_inference'])
            self.assertTrue(runtime['auth']['skip_api_key_verification'])
            models = json.loads(settings_path.read_text())['models']
            self.assertEqual(len(models), 1)
            settings = next(iter(models.values()))
            self.assertEqual(settings['model_alias'], model['api_model'])
            self.assertTrue(settings['is_pinned'])
            self.assertTrue(settings['mtp_enabled'])
            self.assertEqual(settings['max_context_window'], 131072)
            self.assertEqual(settings_path.stat().st_mode & 0o777, 0o600)

    def test_missing_optional_worker_does_not_release_working_ds4(self):
        manager = object.__new__(gateway.ModelGateway)
        manager.models = launcher.profiles()['models']
        manager.closing = False
        manager.current = 'deepseek'
        manager.child = Mock()
        with patch.object(omlx_backend, 'validate', side_effect=ValueError('Not installed')), \
                patch.object(manager, '_stop_owned_child') as stop:
            with self.assertRaisesRegex(ValueError, 'Not installed'):
                manager.ensure_loaded('qwen-omlx')
        stop.assert_not_called()
        self.assertEqual(manager.current, 'deepseek')

    def test_worker_auth_is_added_only_to_upstream_request(self):
        manager = Mock()
        manager.upstream_headers.return_value = {'Authorization': 'Bearer worker-only'}
        handler = gateway.make_handler(manager).__new__(
            gateway.make_handler(manager))
        handler.path = '/v1/chat/completions'
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.wfile = io.BytesIO()
        upstream = MagicMock(status=200, headers={})
        upstream.__enter__.return_value = upstream
        upstream.read1.return_value = b''
        with patch.object(gateway.urllib.request, 'urlopen', return_value=upstream) as open_url:
            handler.forward(b'{}', 'qwen-omlx')
        request = open_url.call_args.args[0]
        self.assertEqual(request.get_header('Authorization'), 'Bearer worker-only')
        self.assertNotIn('worker-only', str(handler.send_header.call_args_list))

    def test_ds4_and_omlx_choices_cannot_switch_an_existing_chat(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = FakeManager(Path(directory))
            manager.models = launcher.profiles()['models']
            manager.chat_models.bind('existing-chat', 'qwen')
            restored = gateway.ChatModelLock(Path(directory) / 'chat-models.json')
            self.assertEqual(restored.model_for('existing-chat'), 'qwen')
            backend = ThreadingHTTPServer(('127.0.0.1', 0), FakeBackend)
            frontend = ThreadingHTTPServer(('127.0.0.1', 0), gateway.make_handler(manager))
            servers = [backend, frontend]
            for server in servers:
                threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                with patch.object(gateway, 'BACKEND_PORT', backend.server_port):
                    url = f'http://127.0.0.1:{frontend.server_port}/v1/chat/completions'
                    payload = {'model': 'qwen3.8-flash-next-omlx', 'messages': []}
                    request = urllib.request.Request(url, data=json.dumps(payload).encode(),
                        headers={gateway.CHAT_ID_HEADER: 'existing-chat'})
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        urllib.request.urlopen(request)
                    self.assertEqual(error.exception.code, 409)
                    self.assertIn('removed', json.load(error.exception)['error']['message'])
                    self.assertEqual(manager.loaded, [])
                    request = urllib.request.Request(url, data=json.dumps(payload).encode())
                    with urllib.request.urlopen(request) as response:
                        self.assertEqual(json.load(response)['body'], payload)
                    self.assertEqual(manager.loaded, ['qwen-omlx'])
            finally:
                for server in servers:
                    server.shutdown()
                    server.server_close()

    def test_external_omlx_process_blocks_another_worker(self):
        for command in ('omlx-server', '/path/python3.11 /path/omlx serve',
                        '/path/python3.11 -m omlx serve', '/path/Python /path/omlx serve'):
            with self.subTest(command=command), \
                    patch.object(launcher.socket.socket, 'connect_ex', return_value=1), \
                    patch.object(launcher.subprocess, 'check_output', return_value=command + '\n'):
                with self.assertRaisesRegex(ValueError, 'already running'):
                    launcher.ensure_idle([8100])

    def test_unrelated_commands_that_mention_omlx_do_not_block_loading(self):
        with patch.object(launcher.socket.socket, 'connect_ex', return_value=1), \
                patch.object(launcher.subprocess, 'check_output',
                             return_value='python3.11 /path/launcher.py engine-setup qwen-omlx\n'):
            launcher.ensure_idle([8100])


if __name__ == '__main__':
    unittest.main()
