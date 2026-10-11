"""One-owner gateway. Start the selected DS4 or oMLX worker on demand."""
import fcntl
from datetime import datetime, timezone
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler
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

import launcher
from http_server import BoundedHTTPServer
from inference_usage import UsageRelay, request_usage
from runtime_config import WORKER_PORT as BACKEND_PORT, gateway_port

ROOT = Path(__file__).resolve().parents[1]
MAX_REQUEST_BYTES = 64 * 1024 * 1024
READY_SECONDS = 240
CHAT_ID_HEADER = 'X-DotAgents-Chat-Id'


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
        config = launcher.profiles()
        known_models = set(config['models']) | set(config.get('retired_models', {}))
        if not isinstance(self.chats, dict) or any(
            not isinstance(chat_id, str) or not isinstance(model, str) or model not in known_models
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
            raise ValueError('Native Metal inference requires macOS.')
        self.models = launcher.configured_models()
        if initial not in self.models:
            raise ValueError('Select deepseek or qwen-omlx.')
        for key, model in self.models.items():
            if model.get('optional'):
                continue
            launcher.check_revision(ROOT / model['source'], model['revision'])
            for artifact in launcher.required_artifacts(model):
                launcher.check_artifact(artifact)
        os.umask(0o077)
        self.state = ROOT / '.local/inference-gateway'
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state.chmod(0o700)
        self.chat_models = ChatModelLock(self.state / 'chat-models.json')
        lock_path = ROOT / '.local/inference.lock'
        self.lock_file = lock_path.open('a+')
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            launcher.ensure_idle([BACKEND_PORT, gateway_port(ROOT)])
        except BaseException:
            self.lock_file.close()
            raise
        self.initial = initial
        self.current = None
        self.child = None
        self.log_thread = None
        self.request_lock = threading.Lock()
        self.worker_lock = threading.Lock()
        self.generating_model = None
        self.closing = False

    def upstream_headers(self, key):
        if self.models[key].get('engine') == 'omlx':
            from omlx_backend import headers
            return headers()
        return {}

    def record_usage(self, key, metrics):
        """Append only engine-reported numbers to this model's private log."""
        stamp = datetime.now(timezone.utc).isoformat(timespec='milliseconds')
        line = f'{stamp} dotagents-speed: {json.dumps(metrics, separators=(",", ":"))}\n'
        try:
            descriptor = os.open(self.state / f'{key}.log', os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(descriptor, line.encode())
            finally:
                os.close(descriptor)
        except OSError:
            pass  # Telemetry must not interrupt a chat response.

    def _stop_owned_child(self):
        with self.worker_lock:
            self._reap_owned_child()

    def _reap_owned_child(self):
        child = self.child
        log_thread = self.log_thread
        self.current = None
        (self.state / 'active.json').unlink(missing_ok=True)
        if child is None:
            return
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=2)
        if log_thread is not None:
            log_thread.join(timeout=1)
        # Keep ownership if termination raises, so shutdown can retry safely.
        self.child = None
        self.log_thread = None

    def _start(self, key):
        model = self.models[key]
        print(f'Gateway: loading {model.get("name", key)}', flush=True)
        environment = dict(os.environ)
        environment['LLM_PORT'] = str(BACKEND_PORT)
        environment['LLM_RUNTIME_ROOT'] = str(ROOT / '.local/runtime' / key)
        environment.pop('DS4_METAL_Q8_MV_NSG', None)
        environment.pop('DS4_METAL_Q8_MV_ROWS', None)
        environment.setdefault('DS4_METAL_MODEL_UNTRACKED', '1')
        environment.update(model.get('environment', {}))
        args = launcher.server_args(key, model, environment)
        log = (self.state / f'{key}.log').open('ab', buffering=0)
        try:
            with self.worker_lock:
                if self.closing:
                    raise RuntimeError('Gateway is shutting down')
                child = subprocess.Popen(args, cwd=ROOT / model['source'], env=environment,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         close_fds=True)
                self.child = child
                self.log_thread = threading.Thread(
                    target=relay_ds4_output, args=(child.stdout, log, sys.stdout.buffer),
                    name=f'{key}-logs', daemon=True)
                self.log_thread.start()
        except BaseException:
            log.close()
            raise
        deadline = time.monotonic() + READY_SECONDS
        while time.monotonic() < deadline:
            if self.closing:
                raise RuntimeError('Gateway is shutting down')
            if child.poll() is not None:
                raise RuntimeError(f'{key} exited during startup')
            try:
                request = urllib.request.Request(f'http://127.0.0.1:{BACKEND_PORT}/v1/models',
                                                 headers=self.upstream_headers(key))
                with urllib.request.urlopen(request, timeout=2) as response:
                    ids = {item['id'] for item in json.load(response)['data']}
                if model['api_model'] in ids:
                    if model.get('engine') == 'omlx':
                        request = urllib.request.Request(f'http://127.0.0.1:{BACKEND_PORT}/health',
                                                         headers=self.upstream_headers(key))
                        with urllib.request.urlopen(request, timeout=2) as response:
                            health = json.load(response)
                        if health.get('engine_pool', {}).get('loaded_count') != 1:
                            raise RuntimeError('oMLX did not load its pinned Qwen model')
                    (self.state / 'active.json').write_text(json.dumps(
                        {'model': key, 'engine': model.get('engine', 'ds4'), 'pid': child.pid}) + '\n')
                    self.current = key
                    print(f'Gateway: loaded {model.get("name", key)}', flush=True)
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
        if self.models[key].get('engine') == 'omlx':
            # Validate optional installation before releasing a working DS4 worker.
            from omlx_backend import validate
            validate(self.models[key])
        previous = self.current
        self._stop_owned_child()
        try:
            self._start(key)
        except BaseException:
            self._stop_owned_child()
            if previous and previous != key and not self.closing:
                try:
                    self._start(previous)
                except Exception:
                    self._stop_owned_child()
            raise

    def shutdown(self):
        self.closing = True
        # Interrupt upstream inference before waiting for its request lock.
        # Otherwise a stalled response can hold shutdown for the full HTTP timeout.
        self._stop_owned_child()
        with self.request_lock:
            self._stop_owned_child()
            (self.state / 'active.json').unlink(missing_ok=True)
        self.lock_file.close()


def make_handler(manager):
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
                self.send_json(200, {'object': 'list', 'data': [
                    {'id': model['api_model'], 'name': model['name'],
                     'engine': model.get('engine', 'ds4'),
                     'object': 'model', 'owned_by': 'dotagents',
                     'info': {'meta': {'capabilities': {
                         'vision': bool(model.get('vision_encoder') or model.get('vision')),
                         'file_upload': True}}}} for model in manager.models.values()]})
            elif self.path == '/health':
                child = getattr(manager, 'child', None)
                current = manager.current if child is not None and child.poll() is None else None
                self.send_json(200, {'gateway': 'ready', 'active_model': current,
                                    'generating_model': getattr(manager, 'generating_model', None)
                                    if current is not None else None})
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
                key = next((key for key, model in manager.models.items()
                            if model['api_model'] == requested), None)
                if key is None:
                    raise ValueError('Specify an enabled model ID from /v1/models.')
                with manager.request_lock:
                    # Accept clients that still use the previous project name.
                    chat_id = self.headers.get(
                        CHAT_ID_HEADER, self.headers.get('X-SealedLLM-Chat-Id',
                        self.headers.get('X-Cook-Studio-Chat-Id', ''))).strip()
                    if len(chat_id) > 256:
                        raise ValueError('Invalid chat ID')
                    locked_model = manager.chat_models.model_for(chat_id)
                    if locked_model and locked_model != key:
                        current = manager.models.get(locked_model, {}).get('name')
                        if current is None:
                            current = launcher.profiles().get('retired_models', {}).get(
                                locked_model, locked_model) + ' (removed)'
                        self.send_json(409, {'error': {'message':
                            f'This chat uses {current}. Start a new chat to use another model or engine.'}})
                        return
                    manager.ensure_loaded(key)
                    manager.chat_models.bind(chat_id, key)
                    manager.generating_model = key
                    try:
                        self.forward(body, key)
                    finally:
                        manager.generating_model = None
            except ValueError as error:
                self.send_json(400, {'error': str(error)})
            except (OSError, HTTPException, RuntimeError, subprocess.SubprocessError) as error:
                self.send_json(503, {'error': f'Model could not start or answer: {error}'})

        def forward(self, body, key):
            headers = {'Content-Type': 'application/json'}
            models = getattr(manager, 'models', {})
            omlx = isinstance(models, dict) and models.get(key, {}).get('engine') == 'omlx'
            hide_usage = False
            if omlx:
                body, hide_usage = request_usage(body)
            if manager is not None and hasattr(manager, 'upstream_headers'):
                headers.update(manager.upstream_headers(key))
            request = urllib.request.Request(
                f'http://127.0.0.1:{BACKEND_PORT}{self.path}', data=body,
                headers=headers, method='POST')
            try:
                upstream = urllib.request.urlopen(request, timeout=600)
            except urllib.error.HTTPError as error:
                upstream = error
            with upstream:
                relay = (UsageRelay(
                    upstream.headers.get('Content-Type', '').startswith('text/event-stream'),
                    lambda metrics: manager.record_usage(key, metrics), hide_usage)
                    if omlx and upstream.status == 200 else None)
                try:
                    self.send_response(upstream.status)
                    self.send_header('Content-Type', upstream.headers.get('Content-Type', 'application/json'))
                    self.send_header('Cache-Control', 'no-cache')
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    while chunk := upstream.read1(32768):
                        chunk = relay.feed(chunk) if relay else chunk
                        if chunk:
                            self.wfile.write(chunk)
                            self.wfile.flush()
                    if relay:
                        remainder = relay.finish()
                        if remainder:
                            self.wfile.write(remainder)
                            self.wfile.flush()
                except (OSError, HTTPException):
                    # Headers may already be on the wire. End the incomplete
                    # response without appending a second HTTP response.
                    self.close_connection = True
    return Handler


def main(initial=None, gateway=None):
    initial, _ = launcher.model_profile(initial or (sys.argv[1] if len(sys.argv) > 1 else None))
    gateway = gateway or ModelGateway(initial)
    server = None
    thread = None
    serving = False
    stopping = threading.Event()
    try:
        server = BoundedHTTPServer(('127.0.0.1', gateway_port(ROOT)), make_handler(gateway),
                                  client_timeout=10)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        serving = True
        def stop(_signal, _frame):
            gateway.closing = True
            stopping.set()
        for signal_name in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signal_name, stop)
        try:
            with gateway.request_lock:
                gateway.ensure_loaded(initial)
        except RuntimeError:
            if not gateway.closing:
                raise
        while not stopping.wait(1):
            pass
    finally:
        if serving:
            server.shutdown()
        if server is not None:
            server.server_close()
        if serving:
            thread.join()
        gateway.shutdown()
        print('Gateway: stopped', flush=True)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f'Gateway error: {error}', file=sys.stderr)
        sys.exit(1)
