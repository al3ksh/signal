"""Web server control-plane tests against a fake runner socket. Nothing here talks to Docker or git."""
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import socketserver
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch
import server


class FakeRunnerHandler(socketserver.StreamRequestHandler):
    def handle(self):
        request = json.loads(self.rfile.readline())
        self.server.requests.append(request)
        response = self.server.respond(request)
        self.wfile.write(json.dumps(response).encode() + b'\n')


class FakeRunner(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path):
        super().__init__(path, FakeRunnerHandler)
        self.requests = []
        self.apps = [
            {'id': 'portfolio', 'name': 'PortfolioOS', 'hasRepo': True, 'mode': 'build', 'branch': 'main',
             'git': {'headShort': 'abc1234', 'behind': 2, 'commits': [{'sha': 'def5678', 'subject': 'Fix', 'author': 'a', 'time': 1}]},
             'lastJob': None, 'activeJob': None},
            {'id': 'lights-out', 'name': 'Lights Out', 'hasRepo': False, 'mode': 'build', 'branch': None, 'git': None, 'lastJob': None, 'activeJob': None},
        ]

    def respond(self, request):
        op = request.get('op')
        if op == 'list':
            return {'ok': True, 'apps': self.apps}
        if op == 'run':
            if request.get('app') not in {a['id'] for a in self.apps}:
                return {'ok': False, 'code': 'not_found', 'error': 'Unknown app.'}
            return {'ok': True, 'job': {'id': 'f'*32, 'app': request['app'], 'action': request['action'], 'status': 'queued'}}
        if op == 'jobs':
            if request.get('app') not in {a['id'] for a in self.apps}:
                return {'ok': False, 'code': 'not_found', 'error': 'Unknown app.'}
            return {'ok': True, 'jobs': [{'id': 'f'*32, 'app': request['app'], 'action': 'restart', 'status': 'succeeded'}]}
        if op == 'job':
            return {'ok': True, 'job': {'id': request['id'], 'status': 'running'}}
        if op == 'log':
            return {'ok': True, 'data': 'line\n', 'offset': request['offset'] + 5, 'done': False}
        if op == 'cancel':
            return {'ok': True, 'job': {'id': request['id'], 'status': 'cancelled'}}
        return {'ok': False, 'code': 'invalid', 'error': 'Unknown operation.'}


class ControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.auth = {'salt': '12'*32, 'iterations': 1000, 'secret': 'ab'*32}
        cls.auth['hash'] = hashlib.pbkdf2_hmac('sha256', b'test-password', bytes.fromhex(cls.auth['salt']), 1000).hex()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.socket = os.path.join(cls.tmp.name, 'control.sock')
        cls.runner = FakeRunner(cls.socket)
        threading.Thread(target=cls.runner.serve_forever, daemon=True).start()
        cls.patches = [patch.object(server, 'AUTH', cls.auth)]
        for item in cls.patches:
            item.start()
        cls.httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.url = 'http://127.0.0.1:' + str(cls.httpd.server_port)
        cls.host = '127.0.0.1:' + str(cls.httpd.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.runner.shutdown()
        cls.runner.server_close()
        for item in cls.patches:
            item.stop()
        cls.tmp.cleanup()

    def setUp(self):
        self.httpd.monitor = type('Monitor', (), {'lock': threading.Lock(), 'events': deque(maxlen=100), 'snapshot': {'containers': [
            {'id': 'a'*12, 'name': 'portfolio-web-1', 'project': 'portfolio', 'service': 'web', 'state': 'running', 'health': None},
            {'id': 'b'*12, 'name': 'portfolio-db-1', 'project': 'portfolio', 'service': 'db', 'state': 'exited', 'health': None}]}})()
        self.runner.requests.clear()
        server.CONTROL_ATTEMPTS.clear()
        server.CONTROL_RATE.clear()
        server.CONTROL_CACHE.clear()
        enabled = patch.object(server, 'CONTROL_SOCKET', self.socket)
        enabled.start()
        self.addCleanup(enabled.stop)
        self.session = 'signal_session=' + server.make_session()

    def request(self, path, method='GET', body=None, headers=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.url + path, data=data, headers=headers or {}, method=method)
        try:
            response = urllib.request.urlopen(req)
        except urllib.error.HTTPError as e:
            response = e
        response.body = response.read()
        return response

    def get(self, path, **headers):
        return self.request(path, headers={'Cookie': self.session, **headers})

    # -- phase 1: read-only awareness ------------------------------------------
    def test_disabled_control_hides_endpoints_and_reports_unavailable(self):
        with patch.object(server, 'CONTROL_SOCKET', None):
            for path in ['/api/apps', '/api/apps/portfolio/jobs', '/api/jobs/' + 'f'*32, '/api/jobs/' + 'f'*32 + '/log?offset=0']:
                with self.subTest(path=path):
                    self.assertEqual(self.get(path).status, 404)
            session = json.loads(self.get('/api/session').body)
            self.assertEqual(session['control'], {'available': False, 'allowed': False, 'granted': False})
        self.assertEqual(self.runner.requests, [])

    def test_apps_require_session(self):
        self.assertEqual(self.request('/api/apps').status, 401)

    def test_apps_join_registry_with_container_state(self):
        response = self.get('/api/apps')
        self.assertEqual(response.status, 200)
        data = json.loads(response.body)
        portfolio = next(a for a in data['apps'] if a['id'] == 'portfolio')
        self.assertEqual(portfolio['containers'], {'running': 1, 'total': 2, 'unhealthy': 0, 'items': [
            {'name': 'portfolio-db-1', 'service': 'db', 'state': 'exited', 'health': None},
            {'name': 'portfolio-web-1', 'service': 'web', 'state': 'running', 'health': None}]})
        self.assertEqual(portfolio['git']['behind'], 2)
        self.assertEqual(next(a for a in data['apps'] if a['id'] == 'lights-out')['containers']['total'], 0)
        self.assertEqual(data['control'], {'available': True, 'allowed': True, 'granted': False})

    def test_session_reports_control_availability(self):
        session = json.loads(self.get('/api/session').body)
        self.assertEqual(session['control'], {'available': True, 'allowed': True, 'granted': False})

    def test_runner_down_is_unavailable_not_an_error_page(self):
        with patch.object(server, 'CONTROL_SOCKET', os.path.join(self.tmp.name, 'missing.sock')):
            self.assertEqual(self.get('/api/apps').status, 503)
            self.assertFalse(json.loads(self.get('/api/session').body)['control']['available'])

    def test_tunnel_requests_are_read_only(self):
        session = json.loads(self.get('/api/session', **{'Cf-Ray': 'x', 'CF-Connecting-IP': '100.64.0.9'}).body)
        self.assertEqual(session['control'], {'available': True, 'allowed': False, 'granted': False})
        data = json.loads(self.get('/api/apps', **{'CF-Connecting-IP': '203.0.113.9'}).body)
        self.assertFalse(data['control']['allowed'])
        self.assertEqual(len(data['apps']), 2)

    # -- phase 2: lifecycle mutations ------------------------------------------
    def post(self, path, body=None, password=None, **headers):
        body = dict(body or {})
        if password is not None:
            body['password'] = password
        base = {'Cookie': self.session, 'Origin': 'http://' + self.host, 'Content-Type': 'application/json'}
        return self.request(path, 'POST', body, {**base, **headers})

    def runs(self):
        return [r for r in self.runner.requests if r.get('op') == 'run']

    def control_events(self):
        return [e for e in self.httpd.monitor.events if e.get('kind') == 'control']

    def test_disabled_control_mutations_are_404(self):
        with patch.object(server, 'CONTROL_SOCKET', None):
            self.assertEqual(self.post('/api/apps/portfolio/restart', password='test-password').status, 404)
        self.assertEqual(self.runner.requests, [])

    def test_mutation_requires_session(self):
        response = self.request('/api/apps/portfolio/restart', 'POST', {'password': 'test-password'}, {'Origin': 'http://' + self.host, 'Content-Type': 'application/json'})
        self.assertEqual(response.status, 401)
        self.assertEqual(self.runs(), [])

    def test_unauthenticated_refusals_do_not_flood_the_journal(self):
        for _ in range(3):
            self.request('/api/apps/portfolio/restart', 'POST', {}, {'Origin': 'http://' + self.host, 'Content-Type': 'application/json', 'Cf-Ray': 'x'})
            self.request('/api/apps/portfolio/restart', 'POST', {}, {'Origin': 'https://evil.example'})
        self.assertEqual(self.control_events(), [])

    def test_tunnel_requests_cannot_mutate(self):
        response = self.post('/api/apps/portfolio/restart', password='test-password', **{'Cf-Ray': 'abc', 'CF-Connecting-IP': '100.100.1.1'})
        self.assertEqual(response.status, 403)
        self.assertEqual(self.runs(), [])
        event = self.control_events()[0]
        self.assertEqual((event['name'], event['state'], event['outcome']), ('portfolio', 'restart', 'refused'))

    def test_networks_outside_allow_list_cannot_mutate(self):
        with patch.object(server, 'CONTROL_ALLOW', [server.ipaddress.ip_network('100.64.0.0/10')]):
            self.assertEqual(self.post('/api/apps/portfolio/restart', password='test-password').status, 403)
            self.assertFalse(json.loads(self.get('/api/session').body)['control']['allowed'])
        self.assertEqual(self.runs(), [])

    def test_csrf_protection(self):
        for headers in [{'Origin': 'https://evil.example'}, {'Origin': ''}, {'Origin': '', 'Sec-Fetch-Site': 'cross-site'},
                        {'Content-Type': 'text/plain'}]:
            with self.subTest(headers=headers):
                self.assertEqual(self.post('/api/apps/portfolio/restart', password='test-password', **headers).status, 403)
        self.assertEqual(self.runs(), [])
        self.assertEqual(self.post('/api/apps/portfolio/restart', password='test-password', Origin='', **{'Sec-Fetch-Site': 'same-origin'}).status, 202)

    def test_step_up_password_and_grant(self):
        response = self.post('/api/apps/portfolio/restart')
        self.assertEqual(response.status, 403)
        self.assertTrue(json.loads(response.body)['stepUp'])
        self.assertEqual(self.post('/api/apps/portfolio/restart', password='wrong').status, 403)
        self.assertEqual(self.runs(), [])
        response = self.post('/api/apps/portfolio/restart', password='test-password')
        self.assertEqual(response.status, 202)
        self.assertEqual(json.loads(response.body)['job']['action'], 'restart')
        cookie = response.headers['Set-Cookie']
        for flag in ['signal_control=', 'HttpOnly', 'SameSite=Strict', 'Max-Age=600']:
            self.assertIn(flag, cookie)
        grant = cookie.split(';')[0]
        both = self.session + '; ' + grant
        self.assertEqual(self.post('/api/apps/portfolio/stop', Cookie=both).status, 202)
        self.assertTrue(json.loads(self.get('/api/session', Cookie=both).body)['control']['granted'])
        other_session = 'signal_session=' + server.make_session() + '; ' + grant
        self.assertEqual(self.post('/api/apps/portfolio/stop', Cookie=other_session).status, 403)
        expired = 'signal_control=' + server.make_control_grant(self.session.split('=', 1)[1], int(server.time.time()) - 1)
        self.assertEqual(self.post('/api/apps/portfolio/stop', Cookie=self.session + '; ' + expired).status, 403)

    def test_check_does_not_need_step_up(self):
        self.assertEqual(self.post('/api/apps/portfolio/check').status, 202)

    def test_wrong_password_is_rate_limited(self):
        for _ in range(5):
            self.assertEqual(self.post('/api/apps/portfolio/restart', password='wrong').status, 403)
        self.assertEqual(self.post('/api/apps/portfolio/restart', password='test-password').status, 429)
        self.assertEqual(self.runs(), [])

    def test_mutations_are_rate_limited(self):
        statuses = [self.post('/api/apps/portfolio/check').status for _ in range(server.CONTROL_RATE_LIMIT + 1)]
        self.assertEqual(statuses[-1], 429)
        self.assertEqual(set(statuses[:-1]), {202})

    def test_unknown_app_and_action(self):
        self.assertEqual(self.post('/api/apps/nope/restart', password='test-password').status, 404)
        self.assertEqual(self.post('/api/apps/portfolio/exec', password='test-password').status, 404)
        self.assertEqual(self.post('/api/apps/Portfolio/restart', password='test-password').status, 404)
        self.assertEqual(self.runs(), [])

    def test_only_validated_id_and_action_reach_the_runner(self):
        body = {'app': '../../etc', 'action': 'exec', 'cmd': 'rm -rf /', 'args': ['--privileged'], 'actor': 'root'}
        self.assertEqual(self.post('/api/apps/portfolio/restart', body, password='test-password').status, 202)
        run = self.runs()[0]
        self.assertEqual(set(run), {'op', 'app', 'action', 'actor'})
        self.assertEqual((run['app'], run['action']), ('portfolio', 'restart'))
        self.assertEqual(run['actor'], 'session@127.0.0.1')

    def test_audit_records_accepted_and_refused(self):
        self.post('/api/apps/portfolio/restart', password='wrong')
        self.post('/api/apps/portfolio/restart', password='test-password')
        outcomes = [(e['state'], e['outcome']) for e in self.control_events()]
        self.assertEqual(outcomes, [('restart', 'accepted'), ('restart', 'refused')])
        self.assertIn('password', self.control_events()[1]['detail'].lower())

    def test_job_endpoints_forward_to_runner(self):
        job = 'f' * 32
        self.assertEqual(json.loads(self.get('/api/apps/portfolio/jobs').body)['jobs'][0]['id'], job)
        self.assertEqual(json.loads(self.get('/api/jobs/' + job).body)['job']['status'], 'running')
        log = json.loads(self.get('/api/jobs/' + job + '/log?offset=7').body)
        self.assertEqual((log['data'], log['offset']), ('line\n', 12))
        self.get('/api/jobs/' + job + '/log?offset=7&epoch=2')
        self.assertEqual([r for r in self.runner.requests if r['op'] == 'log'][-1], {'op': 'log', 'id': job, 'offset': 7, 'epoch': 2})
        self.assertEqual(self.get('/api/jobs/' + job + '/log?offset=7&epoch=x').status, 400)
        self.assertEqual(self.get('/api/jobs/not-a-job').status, 404)
        self.assertEqual(self.get('/api/apps/nope/jobs').status, 404)
        self.assertEqual(self.post('/api/jobs/' + job + '/cancel', password='test-password').status, 200)


if __name__ == '__main__':
    unittest.main()
