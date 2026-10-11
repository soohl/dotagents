"""Read service logs without managing the services that produce them."""
import asyncio
from dataclasses import dataclass
import re
import shutil
import time

from jobs import OwnedJob


@dataclass
class Reader:
    source: str
    args: list
    task: asyncio.Task | None = None
    retry_at: float = 0


class LineBuffer:
    def __init__(self, emit, limit=4096):
        self.emit, self.limit, self.pending = emit, limit, ''

    def feed(self, chunk):
        lines = (self.pending + chunk.replace('\r', '\n')).split('\n')
        self.pending = lines.pop()
        for line in lines:
            if line:
                self.emit(line[:self.limit] + (' … [truncated]' if len(line) > self.limit else ''))
        if len(self.pending) > self.limit:
            self.emit(self.pending[:self.limit] + ' … [truncated]')
            self.pending = ''

    def finish(self):
        if self.pending:
            self.emit(self.pending)
            self.pending = ''


class ServiceLogs:
    def __init__(self, root, emit):
        self.root, self.emit = root, emit
        self.readers = {}
        self.positions = {}
        self.last_lines = {}
        self.closed = False
        self.lock = asyncio.Lock()
        self.sync_task = None

    def desired(self, containers):
        sources = {}
        docker = shutil.which('docker')
        if docker:
            for container in containers:
                identity = container.get('container_id', '')
                if not re.fullmatch(r'[0-9a-f]{12,64}', identity):
                    continue
                state = container['state']
                if state not in ('running', 'exited'):
                    continue
                args = [docker, 'logs', '--timestamps']
                if state == 'running':
                    args.append('--follow')
                sources['docker:' + identity] = (container['name'], args + [identity])
        tail = shutil.which('tail')
        if tail:
            for path in sorted((self.root / '.local/inference-gateway').glob('*.log')):
                if path.is_file():
                    sources['file:' + path.name] = ('gateway' if path.name == 'gateway.log' else 'inference/' + path.stem,
                                                    [tail, '-n', '10', '-F', str(path)])
        return sources

    def sync(self, containers):
        if self.closed or self.sync_task and not self.sync_task.done():
            return
        self.sync_task = asyncio.create_task(self.update(containers))
        self.sync_task.add_done_callback(self.sync_finished)

    def sync_finished(self, task):
        if not task.cancelled() and task.exception() is not None and not self.closed:
            self.emit('logs', 'Cannot update service log readers; retrying on the next observation.')

    async def update(self, containers):
        async with self.lock:
            if self.closed:
                return
            desired = self.desired(containers)
            for key, reader in list(self.readers.items()):
                if key not in desired or reader.args != desired[key][1]:
                    reader.task.cancel()
                    await asyncio.gather(reader.task, return_exceptions=True)
                    del self.readers[key]
                    if key not in desired:
                        self.positions.pop(key, None)
                        self.last_lines.pop(key, None)
            for key, (source, args) in desired.items():
                reader = self.readers.get(key)
                if reader and (not reader.task.done() or time.monotonic() < reader.retry_at):
                    continue
                reader = Reader(source, args)
                self.readers[key] = reader
                reader.task = asyncio.create_task(self.follow(key, reader))

    async def follow(self, key, reader):
        docker = key.startswith('docker:')
        def emit(line):
            if reader.source == 'gateway' and not line.startswith('Gateway'):
                return  # Engine output has its own reader and model identity.
            if docker:
                timestamp = line.split(' ', 1)[0]
                if re.fullmatch(r'\d{4}-\d{2}-\d{2}T[0-9:.]+Z', timestamp):
                    self.positions[key] = timestamp
                    if self.last_lines.get(key) == line:
                        return
                    self.last_lines[key] = line
            self.emit(reader.source, line)
        lines = LineBuffer(emit)
        args = list(reader.args)
        if docker:
            options = (['--since', self.positions[key]] if key in self.positions else ['--tail', '20'])
            args[-1:-1] = options
        try:
            code = await OwnedJob().run(args, lines.feed, self.root)
            lines.finish()
            if code:
                self.emit('logs', f'{reader.source}: log reader exited ({code}); retrying')
        except (OSError, ValueError) as error:
            self.emit('logs', f'{reader.source}: {error}')
        finally:
            # Completed stopped-container logs need no repeated replay.
            reader.retry_at = (float('inf') if docker and '--follow' not in args
                               else time.monotonic() + 10)

    async def close(self):
        self.closed = True
        if self.sync_task:
            self.sync_task.cancel()
            await asyncio.gather(self.sync_task, return_exceptions=True)
        async with self.lock:
            tasks = [reader.task for reader in self.readers.values()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.readers.clear()
            self.positions.clear()
            self.last_lines.clear()
