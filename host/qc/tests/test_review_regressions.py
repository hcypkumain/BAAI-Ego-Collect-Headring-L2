"""Astra R1/R2 host acceptance and R3 real loopback SSE reproductions."""
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import socket
import struct
import sys
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
for rel in ('orangepi/src/orangepi_collect/control', 'host/qc/fleet_monitor'):
    sys.path.insert(0, str(ROOT/rel))
from read_model import ReadModel
from server import Handler, History, Hub


class ReviewHostTests(unittest.TestCase):
    def test_r1_verified_identity_does_not_make_underestimated_age_fresh(self):
        value = json.loads((ROOT/'contracts/v1/fixtures/device.json').read_text())
        value['resources']['ram_sample'].update(sampled_mono_ms=0, age_ms=0)
        model = ReadModel(['cp_0015'], clock=lambda:100000)
        with self.assertRaises(ValueError):
            model.accept(model.begin_observation('cp_0015'), json.dumps(value), identity_verified=True, snapshot_age_ms=0)
        self.assertIsNone(model.snapshot()['devices'][0]['snapshot'])

    def test_r2_verified_wire_with_null_proofs_is_rejected(self):
        value = json.loads((ROOT/'contracts/v1/fixtures/device.json').read_text())
        value['session']['profile_epoch'] = None
        value['progress']['total'] = None
        for side in ('left', 'right'):
            value['roles'][side].update(profile_epoch=None, binding_id=None, calibration_version=None)
        value['health']['impedance'].update(measurement_id='m1', owner_run='o1',
            session_id=value['session']['session_id'], profile_epoch=None,
            quality_version='q1', quality='pass', can_review=True)
        model = ReadModel(['cp_0015'], clock=lambda:100000)
        with self.assertRaises(ValueError):
            model.accept(model.begin_observation('cp_0015'), json.dumps(value), identity_verified=True, snapshot_age_ms=0)
        self.assertIsNone(model.snapshot()['devices'][0]['snapshot'])


class ReviewStreamTests(unittest.TestCase):
    def setUp(self):
        self.model = ReadModel(['cp_0015'])
        self.hub = Hub.read_only(self.model)
        self.hub.touch = lambda: self.fail('legacy touch')
        hub = self.hub
        class Stream(Handler):
            def setup(self):
                super().setup()
                self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)
        Stream.hub = hub
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Stream)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.streams = []
        self.addCleanup(self.close_server)
        self.sync = patch.object(History, 'sync', side_effect=AssertionError('History.sync'))
        self.start = patch.object(Hub, 'start', side_effect=AssertionError('Hub.start'))
        self.sync.start(); self.start.start()
        self.addCleanup(self.sync.stop); self.addCleanup(self.start.stop)

    def close_server(self):
        for sock, reader in self.streams:
            reader.close(); sock.close()
        self.hub.stop.set()
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=3)

    def connect(self):
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
        sock.settimeout(3)
        sock.connect(('127.0.0.1', self.server.server_port))
        sock.sendall(b'GET /api/v1/fleet/events HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n')
        reader = sock.makefile('rb', buffering=0)
        self.streams.append((sock, reader))
        self.assertIn(b'200', reader.readline())
        while reader.readline() != b'\r\n': pass
        lines = [reader.readline() for _ in range(4)]
        self.assertEqual(lines[1], b'event: reset\n')
        return sock, reader

    def client_count(self):
        with self.model.lock: return len(self.model.clients)

    def wait_empty(self, timeout=3):
        deadline = time.monotonic() + timeout
        while self.client_count() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertEqual(self.client_count(), 0)

    def test_r3_sixteen_idle_rst_connections_release_slots(self):
        before = (self.model.revision, dict(self.model.issued))
        for _ in range(16): self.connect()
        self.assertEqual(self.client_count(), 16)
        self.assertEqual(self.streams[0][1].readline(), b': keepalive\n')
        self.assertEqual(self.streams[0][1].readline(), b'\n')
        for sock, reader in self.streams:
            reader.close()
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
            sock.close()
        self.streams.clear()
        self.wait_empty()
        self.assertEqual(before, (self.model.revision, self.model.issued))

    def test_r3_seventeenth_http_subscription_is_typed_503(self):
        for _ in range(16): self.connect()
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        try:
            connection.request('GET', '/api/v1/fleet/events')
            response = connection.getresponse()
            self.assertEqual(response.status, 503)
            value = json.loads(response.read())
            self.assertEqual(value['schema_version'], '1.0')
            self.assertEqual(value['error']['code'], 'CAPACITY_EXCEEDED')
            self.assertEqual(self.client_count(), 16)
        finally: connection.close()

    def test_r3_nonreading_socket_has_bounded_write_timeout(self):
        self.connect()  # Stop reading but keep the peer socket open.
        deadline = time.monotonic() + 5
        # Synthetic in-memory fixture changes fill the tiny transport window;
        # no device events, pollers, History or external network are involved.
        while self.client_count() and time.monotonic() < deadline:
            self.model.source_unavailable()
            time.sleep(.001)
        self.assertEqual(self.client_count(), 0)


if __name__ == '__main__': unittest.main()
