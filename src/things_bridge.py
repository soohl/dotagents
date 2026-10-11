"""Authenticated loopback relay to the macOS-only Things stdio MCP."""
import fcntl
import hmac
import json
import os
from pathlib import Path
import secrets
import selectors
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
ADDRESS = ('127.0.0.1', 8766)
KEY = 'DOTAGENTS_THINGS_TOKEN'


def settings(root=ROOT):
    values = {}
    for line in (root / '.env').read_text().splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            key, value = line.split('=', 1)
            values[key.strip()] = value.strip().strip('\"\'')
    return values


def request(mode, root=ROOT, *, address=ADDRESS, key=KEY):
    token = settings(root).get(key)
    if not token:
        return False
    try:
        with socket.create_connection(address, timeout=2) as connection:
            connection.sendall(json.dumps(dict(token=token, mode=mode)).encode() + b'\n')
            with connection.makefile('rb') as stream:
                reply = stream.readline(4096)
            if mode == 'describe':
                try:
                    value = json.loads(reply)
                    return value if isinstance(value, dict) else None
                except ValueError:
                    return None
            return reply == b'OK\n'
    except ConnectionRefusedError:
        return False


def owns_legacy_listener(root, name, address):
    """Recognize this checkout's pre-description bridge before replacing it."""
    from gateway_service import GatewayService
    try:
        result = subprocess.run(['lsof', '-nP', '-a', '-iTCP:' + str(address[1]),
                                 '-sTCP:LISTEN', '-t'], capture_output=True, text=True, timeout=3)
        pids = set(result.stdout.split())
        if result.returncode or len(pids) != 1:
            return False
        rows = GatewayService(root).processes(int(pids.pop()))
        if len(rows) != 1:
            return False
        process = rows[0]
        args = process['command']
        return (process['uid'] == os.getuid() and len(args) == 3
                and Path(args[0]).name.lower().startswith('python')
                and args[1:] == ['-u', str(root / 'src' / (name + '_bridge.py'))])
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def start(root=ROOT, *, name='things', address=ADDRESS, key=KEY, configuration=None):
    root = root.resolve()
    directory = root / '.local' / (name + '-bridge')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / 'control.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not settings(root).get(key):
            with (root / '.env').open('a') as env:
                env.write('\n' + key + '=' + secrets.token_urlsafe(32) + '\n')
            (root / '.env').chmod(0o600)
        if request('health', root, address=address, key=key):
            if configuration is None:
                return
            info = request('describe', root, address=address, key=key)
            owned = (info.get('owner') == str(root) if info is not None else
                     owns_legacy_listener(root, name, address))
            if not owned:
                raise RuntimeError(f'{name.title()} bridge ownership could not be verified. Its listener was not stopped.')
            if info and info.get('configuration') == configuration:
                return
            if not request('shutdown', root, address=address, key=key):
                raise RuntimeError(f'{name.title()} bridge refused shutdown.')
            deadline = time.monotonic() + 10
            while True:
                try:
                    with socket.socket() as probe:
                        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                        probe.bind(address)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError(f'{name.title()} bridge is still shutting down.') from None
                    time.sleep(.1)
        # Refuse an occupied listener instead of stopping or replacing its owner.
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(address)
        with (directory / 'bridge.log').open('ab') as log:
            child = subprocess.Popen([sys.executable, '-u', str(root / 'src' / (name + '_bridge.py'))],
                cwd=root, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        for _ in range(50):
            if child.poll() is not None:
                raise RuntimeError(f'{name.title()} bridge startup failed. Read .local/{name}-bridge/bridge.log.')
            if request('health', root, address=address, key=key):
                if configuration is not None:
                    info = request('describe', root, address=address, key=key)
                    if not info or info.get('owner') != str(root) or info.get('configuration') != configuration:
                        child.terminate()
                        try:
                            child.wait(timeout=8)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            child.wait(timeout=2)
                        raise RuntimeError(f'{name.title()} configuration changed during startup. Retry ./run.sh.')
                return
            time.sleep(.1)
        raise RuntimeError(f'{name.title()} bridge did not become ready.')


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, token, command, environment, label='Things', *, owner=None, configuration=None):
        self.token, self.command, self.environment = token, command, environment
        self.label = label
        self.owner, self.configuration = owner, configuration
        self.slots = threading.BoundedSemaphore(16)
        self.children = set()
        self.children_lock = threading.Lock()
        self.stopping = threading.Event()
        super().__init__(address, Handler)

    def process_request(self, request, address):
        if not self.slots.acquire(False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def handle_error(self, *_):
        print(self.label + ' MCP connection ended with a transport error.', flush=True)


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        connection = self.request
        connection.settimeout(5)
        with connection.makefile('rb', buffering=0) as reader:
            line = reader.readline(512)
        try:
            auth = json.loads(line)
            token = auth.get('token')
            if (not line.endswith(b'\n') or not isinstance(token, str)
                    or not hmac.compare_digest(token, self.server.token)):
                return
        except (ValueError, AttributeError):
            return
        mode = auth.get('mode')
        if mode == 'describe':
            connection.sendall(json.dumps(dict(owner=self.server.owner,
                                              configuration=self.server.configuration)).encode() + b'\n')
            return
        if mode not in ('health', 'shutdown', 'stdio') or self.server.stopping.is_set():
            return
        connection.sendall(b'OK\n')
        if mode == 'shutdown':
            self.server.stopping.set()
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if mode == 'health':
            return
        child = subprocess.Popen(self.server.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=self.server.environment, bufsize=0, start_new_session=True)
        with self.server.children_lock:
            self.server.children.add(child)
        connection.settimeout(None)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(connection, selectors.EVENT_READ, 'client')
                selector.register(child.stdout, selectors.EVENT_READ, 'server')
                while not self.server.stopping.is_set():
                    for key, _ in selector.select(timeout=1):
                        if key.data == 'client':
                            chunk = connection.recv(65536)
                            if not chunk:
                                return
                            child.stdin.write(chunk)
                        else:
                            chunk = os.read(child.stdout.fileno(), 65536)
                            if not chunk:
                                return
                            connection.sendall(chunk)
        except (OSError, ValueError):
            pass
        finally:
            child.stdin.close()
            child.stdout.close()
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
            with self.server.children_lock:
                self.server.children.discard(child)


def main():
    values = settings()
    token = values[KEY]
    uv = shutil.which('uv')
    if not uv:
        raise RuntimeError('Install uv to run the Things MCP.')
    command = [uv, '--directory', str(ROOT / 'mcp/mcp-things-3'), 'run', '--locked', 'things3-mcp']
    environment = {key: os.environ[key] for key in ('HOME', 'USER', 'PATH', 'LANG') if key in os.environ}
    environment.update({key: values[key] for key in ('THINGS_URL_AUTH_TOKEN', 'THINGS_READ_ONLY',
                                                   'THINGS_SHORTCUTS_CONFIG') if key in values})
    with Server(ADDRESS, token, command, environment) as server:
        def shutdown(*_):
            server.stopping.set()
            threading.Thread(target=server.shutdown, daemon=True).start()
        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        print('Things MCP bridge ready on loopback.', flush=True)
        server.serve_forever()
        server.stopping.set()
        deadline = time.monotonic() + 7
        while server.children and time.monotonic() < deadline:
            time.sleep(.1)


if __name__ == '__main__':
    main()
