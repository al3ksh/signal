import hashlib
import json
import struct
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch
import server


class SecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.auth = {'salt': '12'*32, 'iterations': 1000, 'secret': 'ab'*32}
        cls.auth['hash'] = hashlib.pbkdf2_hmac('sha256', b'test-password', bytes.fromhex(cls.auth['salt']), 1000).hex()
        cls.patch = patch.object(server, 'AUTH', cls.auth)
        cls.patch.start()
        cls.httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        cls.httpd.monitor = type('Monitor', (), {'lock': threading.Lock(), 'snapshot': {'containers': [], 'sentinel': 'private'}})()
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.url = 'http://127.0.0.1:' + str(cls.httpd.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.patch.stop()

    def request(self, path, method='GET', body=None, headers=None):
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode() if body is not None else None, headers=headers or {}, method=method)
        try:
            return urllib.request.urlopen(req)
        except urllib.error.HTTPError as e:
            return e

    def test_private_endpoints_require_session(self):
        for path in ['/api/status', '/api/history?range=7d', '/api/alerts', '/api/containers/abcdef123456/logs', '/api/containers/abcdef123456/details']:
            with self.subTest(path=path):
                response = self.request(path)
                self.assertEqual(response.status, 401)
                self.assertNotIn(b'private', response.read())

    def test_session_integrity_and_expiry(self):
        valid = server.make_session()
        self.assertTrue(server.valid_session(valid))
        self.assertFalse(server.valid_session(valid[:-1] + ('1' if valid[-1] != '1' else '2')))
        self.assertFalse(server.valid_session(server.make_session(int(time.time())-1)))
        self.assertFalse(server.valid_session('garbage'))

    def test_login_cookie_and_authenticated_status(self):
        response = self.request('/api/login', 'POST', {'password': 'test-password'}, {'X-Forwarded-Proto':'https'})
        self.assertEqual(response.status, 200)
        cookie = response.headers['Set-Cookie']
        for flag in ['HttpOnly', 'SameSite=Strict', 'Secure', 'Max-Age=2592000']:
            self.assertIn(flag, cookie)
        response = self.request('/api/status', headers={'Cookie': cookie.split(';')[0]})
        self.assertEqual(json.load(response)['sentinel'], 'private')

    def test_rejects_cross_origin_and_wrong_password(self):
        response = self.request('/api/login', 'POST', {'password': 'test-password'}, {'Origin':'https://evil.example'})
        self.assertEqual(response.status, 403)
        self.assertEqual(self.request('/api/login', 'POST', {'password':'wrong'}).status, 401)

    def test_no_docker_mutation_routes(self):
        self.assertEqual(self.request('/api/containers/abcdef123456/restart', 'POST', {}).status, 404)

    def test_ack_requires_private_session_and_same_origin(self):
        self.assertEqual(self.request('/api/alerts/1/ack', 'POST').status, 401)
        self.assertEqual(self.request('/api/alerts/1/ack', 'POST', headers={'Origin':'https://evil.example', 'Cookie':'signal_session='+server.make_session()}).status, 403)

    def test_ack_updates_active_snapshot_immediately(self):
        updated = [{'id': 1, 'acknowledged': 123.0}]
        class FakeStore:
            def acknowledge(self, alert_id): return alert_id == 1
            def alerts(self): return updated
        previous = self.httpd.monitor
        self.httpd.monitor = type('Monitor', (), {
            'lock': threading.Lock(), 'store': FakeStore(),
            'snapshot': {'containers': [], 'alerts': [{'id': 1, 'acknowledged': None}]}
        })()
        try:
            response = self.request('/api/alerts/1/ack', 'POST', headers={'Cookie':'signal_session='+server.make_session()})
            self.assertEqual(response.status, 200)
            self.assertEqual(self.httpd.monitor.snapshot['alerts'], updated)
        finally:
            self.httpd.monitor = previous

    def test_monitoring_preference_requires_session_and_updates_snapshot(self):
        path = '/api/containers/abcdef123456/monitoring'
        self.assertEqual(self.request(path, 'POST', {'muted': True}).status, 401)
        self.assertEqual(self.request(path, 'POST', {'muted': True}, {'Origin':'https://evil.example', 'Cookie':'signal_session='+server.make_session()}).status, 403)

        class FakeStore:
            def alerts(self): return []
        class FakeEngine:
            def __init__(self): self.calls = []
            def set_muted(self, name, muted, now): self.calls.append((name, muted))
        previous = self.httpd.monitor
        engine = FakeEngine()
        self.httpd.monitor = type('Monitor', (), {
            'lock': threading.Lock(), 'store': FakeStore(), 'alert_engine': engine,
            'snapshot': {'containers':[{'id':'abcdef123456','name':'worker','ignoreAlerts':False}], 'alerts':[]}
        })()
        try:
            response = self.request(path, 'POST', {'muted': True}, {'Cookie':'signal_session='+server.make_session()})
            self.assertEqual(response.status, 200)
            self.assertEqual(engine.calls, [('worker', True)])
            self.assertTrue(self.httpd.monitor.snapshot['containers'][0]['monitoringMuted'])
        finally:
            self.httpd.monitor = previous

    def test_history_rejects_unbounded_ranges(self):
        response = self.request('/api/history?range=9999d', headers={'Cookie':'signal_session='+server.make_session()})
        self.assertEqual(response.status, 400)

    def test_health_does_not_expose_telemetry(self):
        response = self.request('/healthz')
        self.assertEqual(json.load(response), {'ok':False})

    def test_no_path_traversal(self):
        self.assertEqual(self.request('/%2e%2e/server.py').status, 403)

    def test_docker_log_demultiplex(self):
        line = b'2026-09-16 hello\n'
        framed = b'\x01\0\0\0' + struct.pack('>I', len(line)) + line
        self.assertEqual(server.decode_logs(framed), line.decode())
        self.assertEqual(server.decode_logs(line), line.decode())

    def test_cpu_idle_delta(self):
        self.assertEqual(server.cpu_percent((100, 50), (200, 100)), 50)
        self.assertEqual(server.cpu_percent((100, 50), (100, 50)), 0)


if __name__ == '__main__':
    unittest.main()
