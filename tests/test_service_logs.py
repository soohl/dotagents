"""Log readers follow output and own only their reader subprocesses."""
import asyncio
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from jobs import OwnedJob
from service_logs import LineBuffer, ServiceLogs


class LogTests(unittest.IsolatedAsyncioTestCase):
    def test_lines_survive_chunks_and_remain_bounded(self):
        result = []
        lines = LineBuffer(result.append, limit=10)
        lines.feed('fir')
        lines.feed('st\nsecond\r')
        lines.feed('x' * 20)
        lines.feed('last')
        lines.finish()
        self.assertEqual(result, ['first', 'second', 'xxxxxxxxxx … [truncated]', 'last'])

    async def test_container_replacement_reconnect_and_close(self):
        with tempfile.TemporaryDirectory() as folder:
            events, calls, cancelled = [], [], []
            ready = asyncio.Event()
            async def run(job, args, output, cwd):
                calls.append(args)
                output('2026-10-02T12:00:00.000000000Z ready\n')
                ready.set()
                try:
                    await asyncio.Future()
                finally:
                    cancelled.append(args[-1])
            logs = ServiceLogs(Path(folder), lambda *line: events.append(line))
            first, second = 'a' * 12, 'b' * 12
            def rows(identity):
                return [{'container_id': identity, 'name': 'caddy', 'state': 'running'}]
            with patch('service_logs.shutil.which', side_effect=lambda name: '/usr/bin/' + name), \
                    patch.object(OwnedJob, 'run', new=run):
                await logs.update(rows(first) + rows('invalid'))
                await asyncio.wait_for(ready.wait(), 2)
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][1:], ['logs', '--timestamps', '--follow', '--tail', '20', first])
                ready.clear()
                task = logs.readers['docker:' + first].task
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                logs.readers['docker:' + first].retry_at = 0
                await logs.update(rows(first))
                await asyncio.wait_for(ready.wait(), 2)
                self.assertIn('--since', calls[-1])
                self.assertEqual(len(events), 1)  # The reconnect boundary is not replayed.
                ready.clear()
                await logs.update(rows(second))
                await asyncio.wait_for(ready.wait(), 2)
                self.assertEqual(list(logs.readers), ['docker:' + second])
                await logs.close()
                self.assertEqual(cancelled, [first, first, second])
                self.assertEqual(logs.readers, {})

    async def test_native_log_append_and_reader_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            directory = root / '.local/inference-gateway'
            directory.mkdir(parents=True)
            path = directory / 'qwen.log'
            path.write_text('initial\n')
            events, jobs = [], []
            arrived = asyncio.Event()
            def emit(source, line):
                events.append((source, line))
                arrived.set()
            def job():
                instance = OwnedJob()
                jobs.append(instance)
                return instance
            logs = ServiceLogs(root, emit)
            with patch('service_logs.OwnedJob', side_effect=job):
                try:
                    await logs.update([])
                    await asyncio.wait_for(arrived.wait(), 5)
                    arrived.clear()
                    with path.open('a') as stream:
                        stream.write('new request\n')
                    await asyncio.wait_for(arrived.wait(), 5)
                    self.assertIn(('inference/qwen', 'new request'), events)
                finally:
                    await logs.close()
            self.assertTrue(jobs)
            self.assertTrue(all(job.process.returncode is not None for job in jobs))
            self.assertTrue(path.exists())


if __name__ == '__main__':
    unittest.main()
