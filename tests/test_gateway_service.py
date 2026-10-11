"""Check gateway lifecycle against live processes and stale ownership records."""
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from gateway_service import GatewayService
from dashboard_inference import watch_dashboard


class GatewayServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / 'src').mkdir()
        self.service = GatewayService(self.root)
        self.service.directory.mkdir(parents=True)

    def spawn(self, name='dashboard_inference.py', legacy=False):
        script = self.root / 'src' / name
        script.write_text('''import fcntl, json, pathlib, signal, subprocess, sys, time
root = pathlib.Path(__file__).resolve().parents[1]
lock = (root / '.local/inference.lock').open('a+')
fcntl.flock(lock, fcntl.LOCK_EX)
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
(root / '.local/inference-gateway/active.json').write_text(json.dumps({'pid': child.pid}))
def stop(*args):
    child.terminate()
    child.wait(timeout=5)
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
print(child.pid, flush=True)
while True: time.sleep(1)
''')
        args = [sys.executable, '-u', str(script)] + (['serve'] if legacy else [])
        process = subprocess.Popen(args, stdout=subprocess.PIPE, text=True)
        def cleanup():
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
            process.stdout.close()
        self.addCleanup(cleanup)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            self.assertTrue(selector.select(5), 'Gateway fixture did not start')
        child = int(process.stdout.readline())
        return process, child

    def test_stop_reaps_registered_gateway_and_its_worker(self):
        process, child = self.spawn()
        self.service.save(self.service.processes(process.pid)[0])
        with self.service:
            self.assertTrue(self.service.owner()['managed'])
            self.assertTrue(self.service.stop(timeout=5))
        self.assertEqual(process.wait(timeout=5), 0)
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)
        self.assertFalse(self.service.record.exists())

    def test_gateway_identity_accepts_the_existing_model_aliases(self):
        process = dict(uid=os.getuid(), command=[sys.executable, '-u',
                       str(self.root / 'src/dotagents.py'), 'serve', 'qwen3.8-flash-next-ds4'])
        self.assertTrue(self.service.is_gateway(process))
        process['command'][-1] = 'unconfigured-model'
        self.assertFalse(self.service.is_gateway(process))

    def test_stop_recognizes_only_the_legacy_gateway_with_its_worker_and_lock(self):
        process, child = self.spawn('dotagents.py', legacy=True)
        with self.service:
            self.assertFalse(self.service.owner()['managed'])
            self.assertTrue(self.service.stop(timeout=5))
        process.wait(timeout=5)
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)

    def test_reused_pid_record_cannot_stop_another_process(self):
        process, _ = self.spawn('unrelated.py')
        record = self.service.processes(process.pid)[0]
        record['started'] = 'different birth time'
        self.service.save(record)
        with self.service:
            self.assertFalse(self.service.stop(timeout=5))
        self.assertIsNone(process.poll())

    def test_even_a_matching_record_cannot_stop_an_unrelated_command(self):
        process, _ = self.spawn('unrelated.py')
        self.service.save(self.service.processes(process.pid)[0])
        with self.service, self.assertRaisesRegex(ValueError, 'another process'):
            self.service.stop(timeout=5)
        self.assertIsNone(process.poll())

    def test_parent_loss_stops_only_the_dashboard_child_group(self):
        done = Mock()
        done.wait.return_value = False
        with patch('dashboard_inference.os.getppid', return_value=1), \
                patch('dashboard_inference.os.getpid', return_value=123), \
                patch('dashboard_inference.os.getpgrp', return_value=123), \
                patch('dashboard_inference.os.killpg') as kill:
            watch_dashboard(456, done)
        self.assertEqual(kill.call_args_list, [unittest.mock.call(123, signal.SIGTERM),
                                                 unittest.mock.call(123, signal.SIGKILL)])

    def test_parent_watcher_reaps_an_engine_after_dashboard_death(self):
        watched = self.root / 'src/watched.py'
        watched.write_text(f'''import os, signal, subprocess, sys, threading, time
sys.path.insert(0, {str(ROOT / 'src')!r})
from dashboard_inference import watch_dashboard
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
def stop(*args):
    child.wait(timeout=5)
    print('stopped', flush=True)
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
parent = os.getppid()
threading.Thread(target=watch_dashboard, args=(parent, threading.Event()), daemon=True).start()
print(os.getpid(), child.pid, flush=True)
while True: time.sleep(1)
''')
        parent_code = ('import subprocess, sys, time; '
                       f'subprocess.Popen([sys.executable, "-u", {str(watched)!r}], start_new_session=True); '
                       'time.sleep(60)')
        parent = subprocess.Popen([sys.executable, '-u', '-c', parent_code],
                                  stdout=subprocess.PIPE, text=True)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(parent.stdout, selectors.EVENT_READ)
                self.assertTrue(selector.select(5), 'Watched worker did not start')
                group, engine = map(int, parent.stdout.readline().split())
                parent.kill()
                parent.wait(timeout=5)
                self.assertTrue(selector.select(5), 'Parent watcher did not stop the engine')
                self.assertEqual(parent.stdout.readline().strip(), 'stopped')
            with self.assertRaises(ProcessLookupError):
                os.kill(engine, 0)
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait(timeout=5)
            if 'group' in locals():
                try:
                    os.killpg(group, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            parent.stdout.close()

    def test_parent_watcher_kills_a_group_that_ignores_graceful_shutdown(self):
        watched = self.root / 'src/stubborn.py'
        watched.write_text(f'''import os, signal, subprocess, sys, threading, time
sys.path.insert(0, {str(ROOT / 'src')!r})
from dashboard_inference import watch_dashboard
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([sys.executable, '-u', '-c',
    'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print("ready"); time.sleep(60)'],
    stdout=subprocess.PIPE, text=True)
assert child.stdout.readline().strip() == 'ready'
parent = os.getppid()
threading.Thread(target=watch_dashboard, args=(parent, threading.Event(), .1), daemon=True).start()
print(os.getpid(), child.pid, flush=True)
while True: time.sleep(1)
''')
        code = ('import subprocess,sys,time; '
                f'subprocess.Popen([sys.executable,"-u",{str(watched)!r}], start_new_session=True); '
                'time.sleep(60)')
        parent = subprocess.Popen([sys.executable, '-u', '-c', code], stdout=subprocess.PIPE, text=True)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(parent.stdout, selectors.EVENT_READ)
                self.assertTrue(selector.select(5))
                group, engine = map(int, parent.stdout.readline().split())
                parent.kill()
                parent.wait(timeout=2)
                self.assertTrue(selector.select(3), 'Owned group did not close its output pipe')
                self.assertEqual(parent.stdout.read(), '')
            for pid in (group, engine):
                rows = self.service.processes(pid)
                self.assertEqual(rows, [], 'Owned process is still running')
        finally:
            if parent.poll() is None:
                parent.kill()
            parent.wait(timeout=2)
            if 'group' in locals():
                try:
                    os.killpg(group, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            parent.stdout.close()


if __name__ == '__main__':
    unittest.main()
