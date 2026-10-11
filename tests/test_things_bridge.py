"""Exercise the local relay without reading or changing Things data."""
from pathlib import Path
import json
import os
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from things_bridge import Server


class ThingsBridgeTests(unittest.TestCase):
    def setUp(self):
        command = [sys.executable, '-u', '-c', 'import sys\nfor line in sys.stdin.buffer:\n sys.stdout.buffer.write(line); sys.stdout.buffer.flush()']
        self.server = Server(('127.0.0.1', 0), 'test-secret', command, dict(os.environ))
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()

    def tearDown(self):
        self.server.stopping.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def connect(self, token='test-secret', mode='stdio'):
        connection = socket.create_connection(self.server.server_address, timeout=3)
        connection.sendall(json.dumps(dict(token=token, mode=mode)).encode() + b'\n')
        return connection

    def test_unauthenticated_connections_cannot_start_an_mcp(self):
        with self.connect('wrong') as connection:
            self.assertEqual(connection.recv(16), b'')
        self.assertEqual(len(self.server.children), 0)
        with self.connect(mode='arbitrary-command') as connection:
            self.assertEqual(connection.recv(16), b'')
        self.assertEqual(len(self.server.children), 0)

    def test_authenticated_relay_preserves_stdio_and_reaps_its_child(self):
        with self.connect() as connection:
            stream = connection.makefile('rb')
            self.assertEqual(stream.readline(), b'OK\n')
            message = b'{"jsonrpc":"2.0","id":1,"method":"initialize"}\n'
            connection.sendall(message)
            self.assertEqual(stream.readline(), message)
            stream.close()
        deadline = time.monotonic() + 3
        while self.server.children and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertEqual(len(self.server.children), 0)

    def test_description_requires_authentication_and_reports_configuration(self):
        self.server.owner = '/fixture'
        self.server.configuration = 'fingerprint'
        with self.connect(mode='describe') as connection:
            response = json.loads(connection.makefile('rb').readline())
            self.assertEqual(response, dict(owner='/fixture', configuration='fingerprint'))
        with self.connect('wrong', mode='describe') as connection:
            self.assertEqual(connection.recv(16), b'')
        self.assertFalse(self.server.children)

    def test_health_does_not_launch_a_worker(self):
        with self.connect(mode='health') as connection:
            self.assertEqual(connection.recv(16), b'OK\n')
        self.assertEqual(len(self.server.children), 0)
