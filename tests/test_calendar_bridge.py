"""Calendar integration uses a separate credential and fails closed on missing access."""
import json
from pathlib import Path
import sys
import tempfile
import socket
import subprocess
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import calendar_bridge
import things_bridge
import verification


class CalendarBridgeTests(unittest.TestCase):
    def test_calendar_bridge_has_distinct_listener_and_secret(self):
        self.assertNotEqual(calendar_bridge.ADDRESS, things_bridge.ADDRESS)
        self.assertEqual(calendar_bridge.ADDRESS[0], '127.0.0.1')
        self.assertNotEqual(calendar_bridge.KEY, things_bridge.KEY)

    def test_calendar_start_uses_its_own_fixed_worker(self):
        root = Path('/fixture')
        with patch.object(calendar_bridge, 'command') as command, \
                patch.object(calendar_bridge, 'load', return_value={'integrations': {'calendar': {
                    'enabled': True, 'calendar_ids': ['allowed']}}}), \
                patch.object(calendar_bridge, 'start_bridge') as start:
            calendar_bridge.start(root)
            command.assert_called_once_with(root)
            start.assert_called_once_with(root, name='calendar', address=calendar_bridge.ADDRESS,
                                          key=calendar_bridge.KEY, configuration=calendar_bridge.fingerprint(
                                              dict(enabled=True, calendar_ids=['allowed'])))

    def test_disabled_calendar_needs_no_native_dependency_or_permission(self):
        config = {'integrations': {'calendar': {'enabled': False}}}
        with patch.object(verification, 'load', return_value=config), \
                patch.object(verification, 'run') as run:
            verification.calendar_integration(Path('/unused'))
            run.assert_not_called()

    def test_calendar_readiness_requires_full_access_without_running_tests(self):
        config = {'integrations': {'calendar': {'enabled': True}}}
        with patch.object(verification, 'load', return_value=config), \
                patch.object(calendar_bridge, 'command', return_value=['/fixture/python']), \
                patch.object(verification.shutil, 'which', return_value='/fixture/uv'), \
                patch.object(verification, 'run') as run:
            run.side_effect = ['', json.dumps({'readable': False, 'authorization': 'write_only'})]
            with self.assertRaisesRegex(ValueError, 'Full Access'):
                verification.calendar_integration(Path('/fixture'))
            self.assertEqual(run.call_count, 2)
            run.reset_mock()
            run.side_effect = ['', json.dumps({'readable': True})]
            verification.calendar_integration(Path('/fixture'))
            self.assertEqual(run.call_count, 2)
            self.assertIn('--check', run.call_args_list[0].args[1])

    def test_reuse_requires_matching_owner_and_calendar_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for owner, fingerprint, expected in ((str(root), 'same', 'reuse'),
                                                 (str(root), 'old', 'restart'),
                                                 ('/another-checkout', 'old', 'refuse')):
                with self.subTest(expected=expected):
                    modes = []
                    def request(mode, *args, **kwargs):
                        modes.append(mode)
                        if mode == 'describe':
                            return dict(owner=owner, configuration=fingerprint if modes.count('describe') == 1 else 'same')
                        return True
                    with patch.object(things_bridge, 'settings', return_value={calendar_bridge.KEY: 'fixture'}), \
                            patch.object(things_bridge, 'request', side_effect=request), \
                            patch.object(things_bridge.socket, 'socket'), \
                            patch.object(things_bridge.subprocess, 'Popen') as spawn:
                        spawn.return_value.poll.return_value = None
                        if expected == 'refuse':
                            with self.assertRaisesRegex(RuntimeError, 'ownership'):
                                things_bridge.start(root, name='calendar', key=calendar_bridge.KEY, configuration='same')
                        else:
                            things_bridge.start(root, name='calendar', key=calendar_bridge.KEY, configuration='same')
                        self.assertEqual(spawn.call_count, int(expected == 'restart'))
                        self.assertEqual('shutdown' in modes, expected == 'restart')

    def test_legacy_bridge_requires_process_ownership_before_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(things_bridge, 'settings', return_value={calendar_bridge.KEY: 'fixture'}), \
                    patch.object(things_bridge, 'request', side_effect=[True, None]) as request, \
                    patch.object(things_bridge, 'owns_legacy_listener', return_value=False), \
                    patch.object(things_bridge.subprocess, 'Popen') as spawn:
                with self.assertRaisesRegex(RuntimeError, 'ownership'):
                    things_bridge.start(root, name='calendar', key=calendar_bridge.KEY, configuration='new')
                self.assertEqual([call.args[0] for call in request.call_args_list], ['health', 'describe'])
                spawn.assert_not_called()

    def test_calendar_fingerprint_changes_for_restrictions_but_not_order(self):
        fingerprint = lambda ids: calendar_bridge.fingerprint(dict(enabled=True, calendar_ids=ids))
        self.assertEqual(fingerprint(['a', 'b']), fingerprint(['b', 'a']))
        self.assertNotEqual(fingerprint([]), fingerprint(['a']))
        self.assertNotEqual(fingerprint(['a', 'b']), fingerprint(['a']))

    def test_live_bridge_restarts_with_new_allowlist_and_reuses_matching_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / 'src').mkdir()
            (root / '.env').write_text('DOTAGENTS_CALENDAR_TOKEN=fixture-secret\n')
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0))
                address = probe.getsockname()
            script = """
import json, os, signal, sys, threading, time
from pathlib import Path
sys.path.insert(0, PROJECT_SOURCE)
from things_bridge import Server
root = Path(__file__).resolve().parents[1]
config = json.loads((root / 'policy.json').read_text())
environment = dict(os.environ, CALENDAR_ALLOWED_IDS=json.dumps(config['ids']))
command = [sys.executable, '-u', '-c', "import os; print(os.environ['CALENDAR_ALLOWED_IDS'])"]
with Server(ADDRESS, 'fixture-secret', command, environment,
            owner=str(root), configuration=config['fingerprint']) as server:
    def stop(*_):
        server.stopping.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    server.serve_forever()
    deadline = time.monotonic() + 7
    while server.children and time.monotonic() < deadline:
        time.sleep(.05)
""".replace('PROJECT_SOURCE', repr(str(Path(things_bridge.__file__).parent)))
            script = script.replace('ADDRESS', repr(address))
            (root / 'src/calendar_bridge.py').write_text(script)
            processes = []
            real_spawn = subprocess.Popen
            def spawn(*args, **kwargs):
                child = real_spawn(*args, **kwargs)
                processes.append(child)
                return child
            def start(ids):
                fingerprint = calendar_bridge.fingerprint(dict(enabled=True, calendar_ids=ids))
                (root / 'policy.json').write_text(json.dumps(dict(ids=ids, fingerprint=fingerprint)))
                things_bridge.start(root, name='calendar', key=calendar_bridge.KEY,
                                    address=address, configuration=fingerprint)
            try:
                with patch.object(things_bridge.subprocess, 'Popen', side_effect=spawn):
                    start([])
                    start([])
                    self.assertEqual(len(processes), 1)
                    start(['restricted'])
                    self.assertEqual(len(processes), 2)
                    processes[0].wait(timeout=3)
                    with socket.create_connection(address, timeout=3) as connection:
                        connection.sendall(json.dumps(dict(token='fixture-secret', mode='stdio')).encode() + b'\n')
                        with connection.makefile('rb') as stream:
                            self.assertEqual(stream.readline(), b'OK\n')
                            self.assertEqual(json.loads(stream.readline()), ['restricted'])
            finally:
                for child in processes:
                    if child.poll() is None:
                        child.terminate()
                    try:
                        child.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=2)

    def test_legacy_listener_must_be_this_users_exact_bridge_script(self):
        from gateway_service import GatewayService
        import os
        root = Path('/fixture')
        process = dict(uid=os.getuid(), command=['python', '-u', '/fixture/src/calendar_bridge.py'])
        with patch.object(things_bridge.subprocess, 'run', return_value=Mock(returncode=0, stdout='123\n')), \
                patch.object(GatewayService, 'processes', return_value=[process]):
            self.assertTrue(things_bridge.owns_legacy_listener(root, 'calendar', calendar_bridge.ADDRESS))
            process['command'][-1] = '/another-checkout/src/calendar_bridge.py'
            self.assertFalse(things_bridge.owns_legacy_listener(root, 'calendar', calendar_bridge.ADDRESS))
            process['command'][-1] = '/fixture/src/calendar_bridge.py'
            process['uid'] += 1
            self.assertFalse(things_bridge.owns_legacy_listener(root, 'calendar', calendar_bridge.ADDRESS))
