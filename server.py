"""SIGNAL: bounded, read-only host/Docker telemetry. Python standard library only."""
import concurrent.futures
import hashlib
import hmac
import http.client
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import socket
import secrets
import struct
import subprocess
import threading
import time
import signal
from storage import Store
from alerts import AlertEngine
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from urllib.parse import urlparse, parse_qs, unquote

ROOT = Path(__file__).resolve().parent
INTERVAL = max(3, int(os.getenv('SIGNAL_INTERVAL', '5')))
DATA_DIR = Path(os.getenv('SIGNAL_DATA_DIR', str(ROOT / 'data')))
PROC_ROOT = Path(os.getenv('SIGNAL_PROC_ROOT', '/proc'))
SYS_ROOT = Path(os.getenv('SIGNAL_SYS_ROOT', '/sys'))
AUTH_FILE = Path(os.getenv('SIGNAL_AUTH_FILE', str(DATA_DIR / 'access.json')))
AUTH = json.loads(AUTH_FILE.read_text()) if AUTH_FILE.exists() else None
PUBLIC_MODE = os.getenv('SIGNAL_PUBLIC_MODE') == '1'
ATTEMPTS = {}
AUTH_LOCK = threading.Lock()


def make_session(expiry=None):
    payload = str(expiry or int(time.time()) + 30*86400) + '.' + secrets.token_hex(16)
    signature = hmac.new(bytes.fromhex(AUTH['secret']), payload.encode(), hashlib.sha256).hexdigest()
    return payload + '.' + signature


def valid_session(token):
    if not AUTH:
        return PUBLIC_MODE
    try:
        payload, signature = token.rsplit('.', 1)
        expected = hmac.new(bytes.fromhex(AUTH['secret']), payload.encode(), hashlib.sha256).hexdigest()
        return int(payload.split('.')[0]) > time.time() and hmac.compare_digest(signature, expected)
    except (ValueError, AttributeError):
        return False


class UnixConnection(http.client.HTTPConnection):
    def __init__(self):
        super().__init__('localhost', timeout=4)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(os.getenv('DOCKER_SOCKET', '/var/run/docker.sock'))


DOCKER_API = None
DOCKER_API_LOCK = threading.Lock()

def docker_get(path, raw=False):
    global DOCKER_API
    if path != '/version' and DOCKER_API is None:
        with DOCKER_API_LOCK:
            if DOCKER_API is None:
                version = docker_get('/version')
                supported = tuple(map(int, version.get('ApiVersion', '1.41').split('.')))
                minimum = tuple(map(int, version.get('MinAPIVersion', '1.24').split('.')))
                chosen = max(minimum, min(supported, (1, 44)))
                if supported < (1, 41):
                    raise RuntimeError('Docker API 1.41 or newer is required.')
                DOCKER_API = '/v' + '.'.join(map(str, chosen))
    conn = UnixConnection()
    try:
        conn.request('GET', ('' if path == '/version' else DOCKER_API) + path)
        response = conn.getresponse()
        body = response.read(512 * 1024)
        if response.status != 200:
            raise RuntimeError('Docker returned HTTP ' + str(response.status))
        return body if raw else json.loads(body)
    finally:
        conn.close()


def read(path, default=''):
    try:
        path = str(path)
        if path.startswith('/proc/'):
            path = PROC_ROOT / path[6:]
        elif path.startswith('/sys/'):
            path = SYS_ROOT / path[5:]
        return Path(path).read_text().strip()
    except (OSError, UnicodeError):
        return default


def cpu_sample():
    result = []
    for line in read('/proc/stat').splitlines():
        if line.startswith('cpu'):
            values = [int(x) for x in line.split()[1:9]]
            result.append((sum(values), values[3] + values[4]))
    return result


def cpu_percent(before, after):
    delta = after[0] - before[0]
    return round(max(0, min(100, 100 * (1 - (after[1] - before[1]) / delta))), 1) if delta > 0 else 0


def network_sample():
    result = {}
    for line in read('/proc/net/dev').splitlines()[2:]:
        name, values = line.split(':', 1)
        name = name.strip()
        if not name.startswith(('lo', 'veth', 'docker', 'br-', 'virbr')):
            values = values.split()
            result[name] = (int(values[0]), int(values[8]))
    return result


def decode_logs(data):
    """Docker multiplexed streams; TTY containers return plain UTF-8."""
    parts, pos = [], 0
    while pos + 8 <= len(data) and data[pos] in (0, 1, 2) and data[pos+1:pos+4] == b'\0\0\0':
        size = struct.unpack('>I', data[pos+4:pos+8])[0]
        parts.append(data[pos+8:pos+8+size])
        pos += 8 + size
    result = b''.join(parts) if parts else data
    return re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', result.decode('utf-8', errors='replace'))[-120000:]


class Monitor:
    def __init__(self):
        self.store = Store(DATA_DIR / 'signal.db')
        self.alert_engine = AlertEngine(self.store)
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.snapshot = None
        self.history = deque(self.store.history(3600)['points'], maxlen=max(720, 3600 // INTERVAL))
        self.events = deque(maxlen=100)
        self.last_cpu = cpu_sample()
        self.last_net = network_sample()
        self.last_time = time.monotonic()
        self.process_ticks = {}
        self.container_ticks = {}
        self.previous_states = {}
        self.boot_at = time.time()

    def process_sample(self, elapsed):
        ticks, processes = {}, []
        hz = os.sysconf('SC_CLK_TCK')
        page = os.sysconf('SC_PAGE_SIZE')
        for folder in PROC_ROOT.iterdir():
            if not folder.name.isdigit():
                continue
            stat = read(folder / 'stat')
            try:
                end = stat.rindex(')')
                name = stat[stat.index('(')+1:end]
                fields = stat[end+2:].split()
                current = int(fields[11]) + int(fields[12])
                key = (folder.name, fields[19])
                previous = self.process_ticks.get(key, current)
                ticks[key] = current
                processes.append({'pid': int(folder.name), 'name': name, 'cpu': round(max(0, current-previous) / hz / elapsed * 100, 1), 'memory': int(fields[21]) * page})
            except (ValueError, IndexError):
                continue
        self.process_ticks = ticks
        return sorted(processes, key=lambda p: (p['cpu'], p['memory']), reverse=True)[:12]

    def container(self, item):
        cid = item['Id']
        result = {'id': cid[:12], 'name': item['Names'][0].lstrip('/'), 'image': item['Image'], 'state': item['State'], 'status': item['Status'], 'created': item['Created'], 'project': item.get('Labels', {}).get('com.docker.compose.project', 'standalone'), 'service': item.get('Labels', {}).get('com.docker.compose.service', ''), 'ports': [{'private': p['PrivatePort'], 'public': p.get('PublicPort'), 'ip': p.get('IP', ''), 'protocol': p['Type']} for p in item.get('Ports', [])], 'cpu': None, 'memory': None, 'memoryLimit': None, 'networkRx': None, 'networkTx': None, 'health': None}
        labels = item.get('Labels', {})
        result['ignoreAlerts'] = labels.get('signal.ignore', '').lower() == 'true' or labels.get('com.docker.compose.oneoff', '').lower() == 'true'
        if '(unhealthy)' in result['status']:
            result['health'] = 'unhealthy'
        elif '(healthy)' in result['status']:
            result['health'] = 'healthy'
        elif 'health: starting' in result['status']:
            result['health'] = 'starting'
        if item['State'] != 'running':
            return result
        try:
            stat = docker_get('/containers/' + cid + '/stats?stream=false&one-shot=true')
            cpu = stat.get('cpu_stats', {})
            current = (cpu.get('cpu_usage', {}).get('total_usage', 0), cpu.get('system_cpu_usage', 0))
            previous = self.container_ticks.get(cid, current)
            self.container_ticks[cid] = current
            delta = current[1] - previous[1]
            result['cpu'] = round(max(0, (current[0]-previous[0]) / delta * cpu.get('online_cpus', 1) * 100), 2) if delta > 0 else None
            mem = stat.get('memory_stats', {})
            result['memory'] = max(0, mem.get('usage', 0) - mem.get('stats', {}).get('inactive_file', 0))
            result['memoryLimit'] = mem.get('limit', 0)
            result['networkRx'] = sum(n.get('rx_bytes', 0) for n in stat.get('networks', {}).values())
            result['networkTx'] = sum(n.get('tx_bytes', 0) for n in stat.get('networks', {}).values())
        except (OSError, ValueError, RuntimeError, http.client.HTTPException):
            result['statsError'] = True
        return result

    def sample(self):
        now, mono = time.time(), time.monotonic()
        elapsed = max(0.1, mono - self.last_time)
        cpu = cpu_sample()
        usage = [cpu_percent(a, b) for a, b in zip(self.last_cpu, cpu)]
        memory = {line.split(':')[0]: int(line.split()[1]) * 1024 for line in read('/proc/meminfo').splitlines()}
        network = network_sample()
        interfaces = [{'name': name, 'rx': max(0, values[0]-self.last_net.get(name, values)[0]) / elapsed, 'tx': max(0, values[1]-self.last_net.get(name, values)[1]) / elapsed, 'totalRx': values[0], 'totalTx': values[1]} for name, values in network.items()]
        disk = shutil.disk_usage(os.getenv('SIGNAL_DISK_PATH', '/'))
        temperatures = []
        for zone in (SYS_ROOT / 'class/thermal').glob('thermal_zone*'):
            try:
                value = float(read(zone / 'temp')) / 1000
                if 0 < value < 150:
                    temperatures.append(value)
            except ValueError:
                pass
        temp = max(temperatures) if temperatures else None
        freq = read('/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq')
        errors, containers, docker_version = [], [], None
        try:
            raw = docker_get('/containers/json?all=1')
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                containers = list(pool.map(self.container, raw))
            docker_version = docker_get('/version').get('Version')
        except (OSError, ValueError, RuntimeError, http.client.HTTPException) as exc:
            errors.append('Docker: ' + str(exc))
        current_states = {c['id']: (c['name'], c['state'], c['health']) for c in containers}
        if not errors:
            for cid, state in current_states.items():
                old = self.previous_states.get(cid)
                if old != state:
                    self.events.appendleft({'time': now, 'name': state[0], 'state': state[1], 'health': state[2], 'kind': 'observed' if not old else 'changed'})
            for cid, old in self.previous_states.items():
                if cid not in current_states:
                    self.events.appendleft({'time': now, 'name': old[0], 'state': 'removed', 'health': None, 'kind': 'changed'})
            self.previous_states = current_states
            self.container_ticks = {k: v for k, v in self.container_ticks.items() if k[:12] in current_states}
        throttle = None
        try:
            raw_throttle = subprocess.run(['vcgencmd', 'get_throttled'], capture_output=True, text=True, timeout=2, check=False).stdout.strip()
            throttle = int(raw_throttle.split('=')[1], 16)
        except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
            pass
        data = {'timestamp': now, 'interval': INTERVAL, 'host': {'name': os.getenv('SIGNAL_NODE_NAME') or read('/proc/sys/kernel/hostname', socket.gethostname()), 'alias': os.getenv('SIGNAL_NODE_NAME') or read('/proc/sys/kernel/hostname', socket.gethostname()), 'model': read('/proc/device-tree/model').replace('\0', '') or read('/sys/class/dmi/id/product_name', 'Linux host'), 'kernel': os.uname().release, 'arch': os.uname().machine, 'uptime': float(read('/proc/uptime', '0').split()[0]), 'cpu': usage[0] if usage else 0, 'cores': usage[1:], 'frequency': int(freq)/1000 if freq else None, 'load': os.getloadavg(), 'temperature': temp, 'throttle': throttle, 'memory': {'total': memory['MemTotal'], 'used': memory['MemTotal']-memory['MemAvailable'], 'available': memory['MemAvailable'], 'cache': memory.get('Cached', 0), 'swapTotal': memory.get('SwapTotal', 0), 'swapUsed': memory.get('SwapTotal', 0)-memory.get('SwapFree', 0)}, 'disk': {'total': disk.total, 'used': disk.used, 'free': disk.free}, 'network': interfaces}, 'containers': sorted(containers, key=lambda c: (c['state'] == 'running' and c['health'] != 'unhealthy', c['name'])), 'dockerVersion': docker_version, 'processes': self.process_sample(elapsed), 'errors': errors, 'startedAt': self.boot_at}
        point = {'time': now, 'cpu': data['host']['cpu'], 'memory': data['host']['memory']['used']/memory['MemTotal']*100, 'temperature': data['host']['temperature'], 'rx': sum(i['rx'] for i in interfaces), 'tx': sum(i['tx'] for i in interfaces)}
        self.store.add(point)
        self.alert_engine.evaluate(data)
        data['alerts'] = self.store.alerts()
        with self.lock:
            self.history.append(point)
            data['history'] = list(self.history)
            data['events'] = list(self.events)
            self.snapshot = data
        self.last_cpu, self.last_net, self.last_time = cpu, network, mono

    def run(self):
        while not self.stop.is_set():
            begin = time.monotonic()
            try:
                self.sample()
            except Exception as exc:
                print('Collector error:', type(exc).__name__, str(exc), flush=True)
            self.stop.wait(max(0.2, INTERVAL - (time.monotonic()-begin)))


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, body, content_type='application/json; charset=utf-8'):
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.send_header('Cache-Control', 'no-store' if content_type.startswith('application/json') else 'no-cache')
        if getattr(self, 'cookie_header', None):
            self.send_header('Set-Cookie', self.cookie_header)
        self.end_headers()
        self.wfile.write(body)

    def json(self, code, payload):
        self.reply(code, json.dumps(payload, ensure_ascii=False).encode())

    def authenticated(self):
        try:
            cookie = SimpleCookie(self.headers.get('Cookie', ''))
            token = cookie.get('signal_session')
            return valid_session(token.value if token else '')
        except Exception:
            return False

    def cookie(self, token, age):
        secure = '; Secure' if self.headers.get('X-Forwarded-Proto') == 'https' else ''
        self.cookie_header = 'signal_session=' + token + '; Path=/; HttpOnly; SameSite=Strict; Max-Age=' + str(age) + secure

    def do_POST(self):
        path = urlparse(self.path).path
        origin = self.headers.get('Origin')
        if origin and urlparse(origin).netloc != self.headers.get('Host'):
            return self.json(403, {'error': 'Request origin is not allowed.'})
        if path == '/api/logout':
            self.cookie('', 0)
            return self.json(200, {'ok': True})
        match = re.fullmatch(r'/api/alerts/(\d+)/ack', path)
        if match:
            if not AUTH or not self.authenticated():
                return self.json(401, {'error': 'A private session is required.'})
            found = self.server.monitor.store.acknowledge(int(match[1]))
            if found:
                alerts = self.server.monitor.store.alerts()
                with self.server.monitor.lock:
                    if self.server.monitor.snapshot is not None:
                        self.server.monitor.snapshot['alerts'] = alerts
            return self.json(200 if found else 404, {'ok': found})
        match = re.fullmatch(r'/api/containers/([a-f0-9]{12})/monitoring', path)
        if match:
            if not AUTH or not self.authenticated():
                return self.json(401, {'error': 'A private session is required.'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if length < 1 or length > 4096:
                    return self.json(400, {'error': 'Invalid request.'})
                body = json.loads(self.rfile.read(length))
                muted = body.get('muted')
                if not isinstance(muted, bool):
                    return self.json(400, {'error': 'muted must be a boolean.'})
            except (ValueError, json.JSONDecodeError):
                return self.json(400, {'error': 'Invalid request.'})
            with self.server.monitor.lock:
                snapshot = self.server.monitor.snapshot
                current = next((c for c in (snapshot or {}).get('containers', []) if c['id'] == match.group(1)), None)
                container = dict(current) if current else None
            if not container:
                return self.json(404, {'error': 'Container is no longer available.'})
            if container.get('ignoreAlerts') and not muted:
                return self.json(409, {'error': 'Monitoring is disabled by the container label.'})
            self.server.monitor.alert_engine.set_muted(container['name'], muted, time.time())
            alerts = self.server.monitor.store.alerts()
            with self.server.monitor.lock:
                snapshot = self.server.monitor.snapshot
                current = next((c for c in (snapshot or {}).get('containers', []) if c['id'] == match.group(1)), None)
                if current:
                    current['monitoringMuted'] = muted or current.get('ignoreAlerts', False)
                    current['monitoringMuteSource'] = 'label' if current.get('ignoreAlerts') else ('manual' if muted else None)
                if snapshot is not None:
                    snapshot['alerts'] = alerts
                result = {'ok': True, 'name': container['name'], 'monitoringMuted': muted or container.get('ignoreAlerts', False), 'monitoringMuteSource': 'label' if container.get('ignoreAlerts') else ('manual' if muted else None)}
            return self.json(200, result)
        if path != '/api/login' or not AUTH:
            return self.json(404, {'error': 'Unknown endpoint.'})
        address = self.headers.get('CF-Connecting-IP', self.client_address[0]) if self.client_address[0] in ('127.0.0.1', '::1') else self.client_address[0]
        now = time.time()
        with AUTH_LOCK:
            recent = [t for t in ATTEMPTS.get(address, []) if now-t < 60]
            if len(recent) >= 5:
                return self.json(429, {'error': 'Too many attempts. Try again in a minute.'})
            recent.append(now)
            ATTEMPTS[address] = recent
            for key in list(ATTEMPTS):
                if not ATTEMPTS[key] or now-ATTEMPTS[key][-1] > 60:
                    del ATTEMPTS[key]
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length < 1 or length > 4096:
                return self.json(400, {'error': 'Invalid request.'})
            body = json.loads(self.rfile.read(length))
            password = body.get('password', '')
            if not isinstance(password, str) or len(password) > 1024:
                return self.json(400, {'error': 'Invalid request.'})
            digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(AUTH['salt']), AUTH['iterations']).hex()
            if not hmac.compare_digest(digest, AUTH['hash']):
                return self.json(401, {'error': 'Incorrect password.'})
            with AUTH_LOCK:
                ATTEMPTS.pop(address, None)
            self.cookie(make_session(), 30*86400)
            return self.json(200, {'ok': True})
        except (ValueError, json.JSONDecodeError):
            return self.json(400, {'error': 'Invalid request.'})

    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path == '/healthz':
            with self.server.monitor.lock:
                snapshot = self.server.monitor.snapshot
            healthy = bool(snapshot and time.time()-snapshot.get('timestamp', 0) < max(60, INTERVAL*4))
            return self.json(200 if healthy else 503, {'ok': healthy})
        if path == '/api/session':
            return self.json(200, {'authenticated': self.authenticated(), 'authEnabled': AUTH is not None, 'logs': AUTH is not None})
        if path.startswith('/api/') and not self.authenticated():
            return self.json(401, {'error': 'Sign in to view system data.'})
        if path == '/api/history':
            ranges = {'1h': 3600, '24h': 86400, '7d': 604800}
            value = parse_qs(parsed.query).get('range', ['24h'])[0]
            if value not in ranges:
                return self.json(400, {'error': 'Use range=1h, 24h or 7d.'})
            return self.json(200, self.server.monitor.store.history(ranges[value]))
        if path == '/api/alerts':
            return self.json(200, {'alerts': self.server.monitor.store.alerts()})
        if path == '/api/status':
            with self.server.monitor.lock:
                data = self.server.monitor.snapshot
            if data is None:
                return self.json(503, {'error': 'Collecting the first sample. Try again in a few seconds.'})
            return self.json(200, data)
        match = re.fullmatch(r'/api/containers/([a-f0-9]{12})/(logs|details)', path)
        if match:
            cid, action = match.groups()
            if action == 'logs' and AUTH is None:
                return self.json(403, {'error': 'Logs require a private session.'})
            with self.server.monitor.lock:
                data = self.server.monitor.snapshot
            if not data or cid not in {c['id'] for c in data['containers']}:
                return self.json(404, {'error': 'Container not found.'})
            try:
                if action == 'logs':
                    logs = docker_get('/containers/' + cid + '/logs?stdout=true&stderr=true&timestamps=true&tail=160', raw=True)
                    return self.json(200, {'logs': decode_logs(logs), 'timestamp': time.time()})
                obj = docker_get('/containers/' + cid + '/json')
                state = obj.get('State', {})
                return self.json(200, {'startedAt': state.get('StartedAt'), 'finishedAt': state.get('FinishedAt'), 'exitCode': state.get('ExitCode'), 'restartCount': obj.get('RestartCount'), 'restartPolicy': obj.get('HostConfig', {}).get('RestartPolicy', {}).get('Name'), 'networks': list(obj.get('NetworkSettings', {}).get('Networks', {})), 'mounts': [{'destination': m.get('Destination'), 'type': m.get('Type'), 'rw': m.get('RW')} for m in obj.get('Mounts', [])]})
            except (OSError, ValueError, RuntimeError, http.client.HTTPException):
                return self.json(502, {'error': 'Could not read container data. Please try again.'})
        if path.startswith('/api/'):
            return self.json(404, {'error': 'Unknown endpoint.'})
        base = (ROOT / 'dist').resolve()
        file = (base / path.lstrip('/')).resolve()
        if not file.is_relative_to(base):
            return self.json(403, {'error': 'Access denied.'})
        if path == '/':
            file = base / 'index.html'
        if not file.is_file():
            return self.json(404, {'error': 'File not found.'})
        mime = mimetypes.guess_type(str(file))[0] or 'application/octet-stream'
        if file.suffix == '.js':
            mime = 'application/javascript'
        self.reply(200, file.read_bytes(), mime)

    def log_message(self, fmt, *args):
        if args and str(args[1]) not in ('200', '304'):
            super().log_message(fmt, *args)


if __name__ == '__main__':
    if AUTH is None and not PUBLIC_MODE:
        raise SystemExit('Authentication missing. Run python3 setup_access.py data/access.json first or explicitly set SIGNAL_PUBLIC_MODE=1.')
    monitor = Monitor()
    collector = threading.Thread(target=monitor.run, daemon=True)
    collector.start()
    server = ThreadingHTTPServer((os.getenv('SIGNAL_HOST', '127.0.0.1'), int(os.getenv('SIGNAL_PORT', '8091'))), Handler)
    server.monitor = monitor
    print('SIGNAL listening on %s:%s' % server.server_address, flush=True)
    def shutdown(signum, frame):
        monitor.stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        server.serve_forever()
    finally:
        monitor.stop.set()
        collector.join(timeout=30)
        if not collector.is_alive():
            monitor.store.close()
        server.server_close()
