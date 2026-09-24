"""Exercise real HTTP routing without loading a second large model."""
import importlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import urllib.error
import urllib.request
import unittest
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
gateway = importlib.import_module('inference_gateway')


class FakeBackend(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers['Content-Length']))
        response = json.dumps({'body': json.loads(body), 'path': self.path}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(response)))
        self.end_headers()
        self.wfile.write(response)


class FakeManager:
    models = {'qwen': {'api_model': 'qwen3.8-flash-next', 'name': 'Qwen 3.8 Flash Next'},
              'deepseek': {'api_model': 'deepseek-v4-flash', 'name': 'DeepSeek V4 Flash'}}

    def __init__(self, state):
        self.current = None
        self.request_lock = threading.Lock()
        self.loaded = []
        self.chat_models = gateway.ChatModelLock(state / 'chat-models.json')

    def ensure_loaded(self, key):
        self.current = key
        self.loaded.append(key)


class GatewayRoutingTests(unittest.TestCase):
    def test_failed_switch_and_rollback_are_cleaned_up_before_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = object.__new__(gateway.ModelGateway)
            manager.state = Path(directory)
            manager.models = {key: {'source': 'backends/ds4', 'api_model': key}
                              for key in ('qwen', 'deepseek')}
            manager.closing = False
            manager.current = 'qwen'
            manager.child = Mock()
            manager.child.poll.return_value = None
            original = manager.child
            manager.log_thread = None
            (manager.state / 'active.json').write_text('{"model":"qwen"}')
            children = []

            def spawn(*args, **kwargs):
                child = Mock(pid=1234)
                child.poll.return_value = None
                child.stdout = io.BufferedReader(io.BytesIO(b''))
                children.append(child)
                return child

            with patch.object(gateway.launcher, 'server_args', return_value=['ds4-server']), \
                    patch.object(gateway.subprocess, 'Popen', side_effect=spawn), \
                    patch.object(gateway.sys, 'stdout', SimpleNamespace(buffer=io.BytesIO())):
                with patch.object(gateway, 'READY_SECONDS', 0), \
                        self.assertRaisesRegex(RuntimeError, 'deepseek did not become ready'):
                    manager.ensure_loaded('deepseek')
                self.assertEqual(len(children), 2)
                for child in [original, *children]:
                    child.terminate.assert_called_once()
                self.assertIsNone(manager.child)
                self.assertIsNone(manager.current)
                self.assertFalse((manager.state / 'active.json').exists())
                ready = io.BytesIO(b'{"data":[{"id":"qwen"}]}')
                with patch.object(gateway.urllib.request, 'urlopen', return_value=ready):
                    manager.ensure_loaded('qwen')
                self.assertEqual(len(children), 3)
                self.assertEqual(manager.current, 'qwen')
                manager.ensure_loaded('qwen')
                self.assertEqual(len(children), 3)
                manager._stop_owned_child()

    def test_upstream_failure_sends_only_one_http_response(self):
        for streaming in (False, True):
            with self.subTest(streaming=streaming), tempfile.TemporaryDirectory() as directory:
                manager = FakeManager(Path(directory))
                handler_type = gateway.make_handler(manager, 'qwen')
                handler = handler_type.__new__(handler_type)
                handler.path = '/v1/chat/completions'
                handler.command = 'POST'
                handler.request_version = 'HTTP/1.1'
                handler.requestline = 'POST /v1/chat/completions HTTP/1.1'
                handler.headers = {'Content-Length': '2'}
                handler.rfile = io.BytesIO(b'{}')
                handler.wfile = io.BytesIO()
                handler.close_connection = False
                upstream = MagicMock(status=200, headers={'Content-Type': 'text/event-stream'})
                upstream.__enter__.return_value = upstream
                upstream.read1.side_effect = [b'data: token\n\n', OSError('disconnected')]
                with patch.object(gateway.urllib.request, 'urlopen', return_value=upstream,
                                  side_effect=None if streaming else OSError('unavailable')):
                    handler.do_POST()
                response = handler.wfile.getvalue()
                self.assertEqual(response.count(b'HTTP/1.0 '), 1)
                if streaming:
                    self.assertTrue(response.startswith(b'HTTP/1.0 200'))
                    self.assertTrue(response.endswith(b'data: token\n\n'))
                    self.assertTrue(handler.close_connection)
                else:
                    self.assertTrue(response.startswith(b'HTTP/1.0 503'))

    def test_client_disconnect_does_not_send_an_error_response(self):
        handler_type = gateway.make_handler(None, 'qwen')
        handler = handler_type.__new__(handler_type)
        handler.path = '/v1/chat/completions'
        handler.command = 'POST'
        handler.request_version = 'HTTP/1.1'
        handler.requestline = 'POST /v1/chat/completions HTTP/1.1'
        handler.wfile = Mock()
        # Header write succeeds; the first token write finds a closed client.
        handler.wfile.write.side_effect = [None, BrokenPipeError()]
        upstream = MagicMock(status=200, headers={})
        upstream.__enter__.return_value = upstream
        upstream.read1.return_value = b'data: token\n\n'
        with patch.object(gateway.urllib.request, 'urlopen', return_value=upstream):
            handler.forward(b'{}')
        self.assertTrue(handler.close_connection)
        self.assertEqual(handler.wfile.write.call_count, 2)
        upstream.__exit__.assert_called_once()

    def test_ds4_output_reaches_gateway_terminal_and_private_log(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = object.__new__(gateway.ModelGateway)
            manager.state = Path(directory)
            manager.models = {'qwen': {'source': 'backends/ds4', 'api_model': 'test-qwen',
                                       'environment': {}}}
            manager.child = None
            manager.current = None
            manager.log_thread = None
            child = Mock(pid=1234)
            child.stdout = io.BufferedReader(io.BytesIO(b'ds4 startup\nds4 ready\n'))
            child.poll.return_value = None
            terminal = io.BytesIO()
            model_list = io.BytesIO(b'{"data":[{"id":"test-qwen"}]}')
            with patch.object(gateway.launcher, 'server_args', return_value=['ds4-server']), \
                    patch.object(gateway.subprocess, 'Popen', return_value=child) as popen, \
                    patch.object(gateway.urllib.request, 'urlopen', return_value=model_list), \
                    patch.object(gateway.sys, 'stdout', SimpleNamespace(buffer=terminal)):
                manager._start('qwen')
                manager._stop_owned_child()
            self.assertEqual(popen.call_args.kwargs['stdout'], gateway.subprocess.PIPE)
            self.assertEqual(popen.call_args.kwargs['stderr'], gateway.subprocess.STDOUT)
            self.assertEqual(terminal.getvalue(), b'ds4 startup\nds4 ready\n')
            self.assertEqual((manager.state / 'qwen.log').read_bytes(), terminal.getvalue())
            child.terminate.assert_called_once()

    def test_chat_model_lock_blocks_switch_before_loading(self):
        try:
            backend = ThreadingHTTPServer(('127.0.0.1', 0), FakeBackend)
        except PermissionError:
            self.skipTest('Loopback sockets are unavailable in this sandbox')
        state = tempfile.TemporaryDirectory()
        manager = FakeManager(Path(state.name))
        frontends = [ThreadingHTTPServer(('127.0.0.1', 0), gateway.make_handler(manager, key))
                     for key in manager.models]
        servers = [backend, *frontends]
        threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in servers]
        try:
            for thread in threads:
                thread.start()
            with patch.object(gateway, 'BACKEND_PORT', backend.server_port):
                for server, key in zip(frontends, manager.models):
                    base = f'http://127.0.0.1:{server.server_port}'
                    with urllib.request.urlopen(base + '/v1/models') as response:
                        models = json.load(response)['data']
                    self.assertEqual([item['id'] for item in models], [manager.models[key]['api_model']])
                    self.assertEqual(models[0]['info']['meta']['capabilities'],
                                     {'vision': key == 'qwen', 'file_upload': True})
                self.assertEqual(manager.loaded, [])
                for server, key, chat_id in zip(
                    [frontends[0], frontends[1], frontends[0]],
                    ['qwen', 'deepseek', 'qwen'],
                    ['chat-a', 'chat-b', 'chat-a'],
                ):
                    model = manager.models[key]['api_model']
                    payload = {'model': model, 'messages': [{'role': 'user', 'content': 'Continue this chat.'}]}
                    request = urllib.request.Request(
                        f'http://127.0.0.1:{server.server_port}/v1/chat/completions',
                        data=json.dumps(payload).encode(),
                        headers={'Content-Type': 'application/json', gateway.CHAT_ID_HEADER: chat_id})
                    with urllib.request.urlopen(request) as response:
                        result = json.load(response)
                    self.assertEqual(result['body'], payload)
                self.assertEqual(manager.loaded, ['qwen', 'deepseek', 'qwen'])
                locked = urllib.request.Request(
                    f'http://127.0.0.1:{frontends[1].server_port}/v1/chat/completions',
                    data=json.dumps({'model': 'deepseek-v4-flash'}).encode(),
                    headers={'Content-Type': 'application/json', gateway.CHAT_ID_HEADER: 'chat-a'})
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(locked)
                self.assertEqual(caught.exception.code, 409)
                self.assertIn('Start a new chat', json.load(caught.exception)['error']['message'])
                self.assertEqual(manager.loaded, ['qwen', 'deepseek', 'qwen'])
                restored = gateway.ChatModelLock(Path(state.name) / 'chat-models.json')
                self.assertEqual(restored.model_for('chat-a'), 'qwen')
                self.assertEqual(restored.model_for('chat-b'), 'deepseek')
                bad = urllib.request.Request(
                    f'http://127.0.0.1:{frontends[0].server_port}/v1/chat/completions',
                    data=json.dumps({'model': 'deepseek-v4-flash'}).encode(),
                    headers={'Content-Type': 'application/json'})
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(bad)
                self.assertEqual(caught.exception.code, 400)
        finally:
            for server in servers:
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=2)
            state.cleanup()


if __name__ == '__main__':
    unittest.main()
