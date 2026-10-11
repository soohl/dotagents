"""Authenticated loopback relay to the read-only Apple Calendar stdio MCP."""
import json
import hashlib
import os
import signal
import threading
import time

from runtime_config import load
from things_bridge import ROOT, Server, settings, start as start_bridge

ADDRESS = ('127.0.0.1', 8767)
KEY = 'DOTAGENTS_CALENDAR_TOKEN'


def command(root=ROOT):
    executable = root / 'mcp/calendar-mcp/.venv/bin/python'
    if not executable.is_file():
        raise ValueError('Prepare Calendar MCP with uv sync --locked --directory mcp/calendar-mcp.')
    return [str(executable), '-m', 'calendar_mcp.server']


def start(root=ROOT):
    config = load(root)['integrations']['calendar']
    if not config['enabled']:
        raise ValueError('Calendar integration is disabled in config.yaml.')
    command(root)
    start_bridge(root, name='calendar', address=ADDRESS, key=KEY,
                 configuration=fingerprint(config))


def fingerprint(config):
    return hashlib.sha256(json.dumps(dict(enabled=config['enabled'],
        calendar_ids=sorted(config['calendar_ids'])), sort_keys=True).encode()).hexdigest()


def main():
    config = load(ROOT)['integrations']['calendar']
    if not config['enabled']:
        raise ValueError('Calendar integration is disabled in config.yaml.')
    token = settings()[KEY]
    environment = {key: os.environ[key] for key in ('HOME', 'USER', 'PATH', 'LANG') if key in os.environ}
    environment['CALENDAR_ALLOWED_IDS'] = json.dumps(config['calendar_ids'])
    with Server(ADDRESS, token, command(), environment, label='Calendar',
                owner=str(ROOT.resolve()), configuration=fingerprint(config)) as server:
        def shutdown(*_):
            server.stopping.set()
            threading.Thread(target=server.shutdown, daemon=True).start()
        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        print('Calendar MCP bridge ready on loopback; read-only tools.', flush=True)
        server.serve_forever()
        server.stopping.set()
        deadline = time.monotonic() + 7
        while server.children and time.monotonic() < deadline:
            time.sleep(.1)


if __name__ == '__main__':
    main()
