"""One-owner DS4 gateway. Start the selected native model on demand."""
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

import cook_studio

ROOT = Path(__file__).resolve().parents[1]
BACKEND_PORT = 8100
MAX_REQUEST_BYTES = 64 * 1024 * 1024
READY_SECONDS = 240
CHAT_ID_HEADER = 'X-Cook-Studio-Chat-Id'


def relay_ds4_output(source, saved, terminal):
    """Drain DS4 output to the private log and the gateway terminal."""
    with source, saved:
        while chunk := source.read1(16384):
            try:
                saved.write(chunk)
            except OSError:
                pass
            try:
                terminal.write(chunk)
                terminal.flush()
            except (OSError, ValueError):
                pass


class ChatModelLock:
    """Keep each chat on its first selected model across gateway restarts."""

    def __init__(self, path):
        self.path = Path(path)
        self.chats = json.loads(self.path.read_text()) if self.path.exists() else {}
        if not isinstance(self.chats, dict) or any(
            not isinstance(chat_id, str) or model not in ('qwen', 'deepseek')
            for chat_id, model in self.chats.items()
        ):
            raise ValueError('Invalid chat model lock file')

    def model_for(self, chat_id):
        return self.chats.get(chat_id)

    def bind(self, chat_id, key):
        if not chat_id or chat_id in self.chats:
            return
        updated = {**self.chats, chat_id: key}
        temporary = None
        try:
            with tempfile.NamedTemporaryFile('w', dir=self.path.parent,
                                             prefix='.chat-models-', delete=False) as output:
                temporary = Path(output.name)
                json.dump(updated, output, separators=(',', ':'))
                output.write('\n')
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            self.chats = updated
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


class ModelGateway:
    def __init__(self, initial):
        if platform.system() != 'Darwin':
            raise ValueError('Native DS4 Metal requires macOS.')
        self.models = cook_studio.profiles()['models']
        if initial not in self.models:
            raise ValueError('Select qwen or deepseek.')
        for key, model in self.models.items():
            cook_studio.check_revision(ROOT / model['source'], model['revision'])
            for artifact in cook_studio.required_artifacts(model):
                cook_studio.check_artifact(artifact)
        os.umask(0o077)
        self.state = ROOT / '.local/inference-gateway'
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state.chmod(0o700)
        self.chat_models = ChatModelLock(self.state / 'chat-models.json')
        lock_path = ROOT / '.local/inference.lock'
        self.lock_file = lock_path.open('a+')
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            cook_studio.ensure_idle([BACKEND_PORT, *(m['port'] for m in self.models.values())])
        except BaseException:
            self.lock_file.close()
            raise
        self.initial = initial
        self.current = None
        self.child = None
        self.log_thread = None
        self.request_lock = threading.Lock()
        self.closing = False

    def _stop_owned_child(self):
        child = self.child
        log_thread = self.log_thread
        self.child = None
        self.log_thread = None
        self.current = None
        if child is None:
            return
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=45)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=15)
        if log_thread is not None:
            log_thread.join(timeout=5)

    def _start(self, key):
        model = self.models[key]
        environment = dict(os.environ)
        environment['LLM_PORT'] = str(BACKEND_PORT)
        environment['LLM_RUNTIME_ROOT'] = str(ROOT / '.local/runtime' / key)
        environment.pop('DS4_METAL_Q8_MV_NSG', None)
        environment.pop('DS4_METAL_Q8_MV_ROWS', None)
        environment.setdefault('DS4_METAL_MODEL_UNTRACKED', '1')
        environment.update(model.get('environment', {}))
        args = cook_studio.server_args(key, model, environment)
        log = (self.state / f'{key}.log').open('ab', buffering=0)
        try:
            child = subprocess.Popen(args, cwd=ROOT / model['source'], env=environment,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     close_fds=True)
        except BaseException:
            log.close()
            raise
        self.child = child
        self.current = key
        self.log_thread = threading.Thread(
            target=relay_ds4_output, args=(child.stdout, log, sys.stdout.buffer),
            name=f'{key}-logs', daemon=True)
        self.log_thread.start()
        deadline = time.monotonic() + READY_SECONDS
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise RuntimeError(f'{key} exited during startup')
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{BACKEND_PORT}/v1/models', timeout=2) as response:
                    ids = {item['id'] for item in json.load(response)['data']}
                if model['api_model'] in ids:
                    (self.state / 'active.json').write_text(json.dumps({'model': key, 'pid': child.pid}) + '\n')
                    return
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(0.5)
        raise RuntimeError(f'{key} did not become ready in {READY_SECONDS} seconds')

    def ensure_loaded(self, key):
        # Caller holds request_lock through response streaming. A switch cannot
        # interrupt a chat that is still producing tokens.
        if self.closing:
            raise RuntimeError('Gateway is shutting down')
        if self.current == key and self.child and self.child.poll() is None:
            return
        previous = self.current
        self._stop_owned_child()
        try:
            self._start(key)
        except BaseException:
            self._stop_owned_child()
            if previous and previous != key:
                try:
                    self._start(previous)
                except Exception:
                    pass
            raise

    def shutdown(self):
        self.closing = True
        with self.request_lock:
            self._stop_owned_child()
            (self.state / 'active.json').unlink(missing_ok=True)
        self.lock_file.close()


def make_handler(manager, key):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.0'

        def log_message(self, format, *args):
            # Request paths can contain private prompt data. Keep logs quiet.
            pass

        def send_json(self, status, value):
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == '/v1/models':
                model_id = manager.models[key]['api_model']
                self.send_json(200, {'object': 'list', 'data': [
                    {'id': model_id, 'name': manager.models[key]['name'],
                     'object': 'model', 'owned_by': 'cook-studio',
                     'info': {'meta': {'capabilities': {
                         'vision': key == 'qwen', 'file_upload': True}}}}]})
            elif self.path == '/health':
                self.send_json(200, {'gateway': 'ready', 'active_model': manager.current})
            else:
                self.send_json(404, {'error': 'Unknown endpoint'})

        def do_POST(self):
            if self.path not in ('/v1/chat/completions', '/v1/completions',
                                 '/v1/responses', '/v1/messages'):
                self.send_json(404, {'error': 'Unknown endpoint'})
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= MAX_REQUEST_BYTES:
                    raise ValueError('Invalid request size')
                body = self.rfile.read(length)
                payload = json.loads(body)
                if not isinstance(payload, dict):
                    raise ValueError('Invalid request body')
                requested = payload.get('model')
                if requested and requested != manager.models[key]['api_model']:
                    # Old Qwen chat IDs remain valid in saved chat histories.
                    if key != 'qwen' or requested not in (
                        'qwen3.8-flash-next-chat', 'qwen3.8-flash-next-reasoner'):
                        raise ValueError('Model does not match this endpoint')
                with manager.request_lock:
                    chat_id = self.headers.get(CHAT_ID_HEADER, '').strip()
                    if len(chat_id) > 256:
                        raise ValueError('Invalid chat ID')
                    locked_model = manager.chat_models.model_for(chat_id)
                    if locked_model and locked_model != key:
                        current = 'Qwen 3.8 Flash Next' if locked_model == 'qwen' else 'DeepSeek V4 Flash'
                        self.send_json(409, {'error': {'message':
                            f'This chat uses {current}. Start a new chat to use the other model.'}})
                        return
                    manager.ensure_loaded(key)
                    manager.chat_models.bind(chat_id, key)
                    self.forward(body)
            except ValueError as error:
                self.send_json(400, {'error': str(error)})
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                self.send_json(503, {'error': f'{key} could not start or answer: {error}'})

        def forward(self, body):
            request = urllib.request.Request(
                f'http://127.0.0.1:{BACKEND_PORT}{self.path}', data=body,
                headers={'Content-Type': 'application/json'}, method='POST')
            try:
                upstream = urllib.request.urlopen(request, timeout=600)
            except urllib.error.HTTPError as error:
                upstream = error
            with upstream:
                self.send_response(upstream.status)
                self.send_header('Content-Type', upstream.headers.get('Content-Type', 'application/json'))
                self.send_header('Cache-Control', 'no-cache')
                self.send_header('Connection', 'close')
                self.end_headers()
                while chunk := upstream.read1(32768):
                    self.wfile.write(chunk)
                    self.wfile.flush()
    return Handler


def main():
    initial = sys.argv[1] if len(sys.argv) > 1 else 'qwen'
    gateway = ModelGateway(initial)
    listeners = []
    stopping = threading.Event()
    try:
        for key, model in gateway.models.items():
            server = ThreadingHTTPServer(('127.0.0.1', model['port']), make_handler(gateway, key))
            server.daemon_threads = True
            listeners.append(server)
        for server in listeners:
            threading.Thread(target=server.serve_forever, daemon=True).start()
        for signal_name in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signal_name, lambda _signal, _frame: stopping.set())
        with gateway.request_lock:
            gateway.ensure_loaded(initial)
        while not stopping.wait(1):
            pass
    finally:
        for server in listeners:
            server.shutdown()
            server.server_close()
        gateway.shutdown()


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f'Gateway error: {error}', file=sys.stderr)
        sys.exit(1)
