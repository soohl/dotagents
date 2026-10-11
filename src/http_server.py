"""Bounded HTTP threads with deadlines for idle or stalled clients."""
from http.server import ThreadingHTTPServer
import threading


class BoundedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 16

    def __init__(self, address, handler, *, max_clients=8, client_timeout=3):
        self.clients = threading.BoundedSemaphore(max_clients)
        self.client_timeout = client_timeout
        super().__init__(address, handler)

    def process_request(self, request, address):
        if not self.clients.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.clients.release()
            raise

    def process_request_thread(self, request, address):
        try:
            request.settimeout(self.client_timeout)
            super().process_request_thread(request, address)
        finally:
            self.clients.release()
