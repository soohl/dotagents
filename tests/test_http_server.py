"""HTTP admission stays bounded and idle connections release their slots."""
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import socket
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from http_server import BoundedHTTPServer


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.server.entered.set()

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.release.wait(2)
        self.send_response(200)
        self.end_headers()


class HTTPServerTests(unittest.TestCase):
    def test_idle_clients_time_out_and_do_not_exhaust_admission(self):
        with BoundedHTTPServer(('127.0.0.1', 0), Handler, max_clients=1, client_timeout=.1) as server:
            server.entered, server.release = threading.Event(), threading.Event()
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                with socket.create_connection(server.server_address, timeout=2) as first:
                    self.assertTrue(server.entered.wait(2))
                    self.assertEqual(first.recv(1), b'')
                server.release.set()
                with socket.create_connection(server.server_address, timeout=2) as next_client:
                    next_client.sendall(b'GET / HTTP/1.0\r\n\r\n')
                    self.assertIn(b'200', next_client.recv(100))
            finally:
                server.release.set()
                server.shutdown()
                thread.join(2)

    def test_excess_clients_are_closed_without_starting_another_handler(self):
        with BoundedHTTPServer(('127.0.0.1', 0), Handler, max_clients=1) as server:
            server.entered, server.release = threading.Event(), threading.Event()
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                with socket.create_connection(server.server_address, timeout=2) as first:
                    first.sendall(b'GET / HTTP/1.0\r\n\r\n')
                    self.assertTrue(server.entered.wait(2))
                    with socket.create_connection(server.server_address, timeout=2) as excess:
                        self.assertEqual(excess.recv(1), b'')
                    server.release.set()
                    self.assertIn(b'200', first.recv(100))
            finally:
                server.release.set()
                server.shutdown()
                thread.join(2)


if __name__ == '__main__':
    unittest.main()
