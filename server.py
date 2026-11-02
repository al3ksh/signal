"""SIGNAL: bounded, read-only host/Docker telemetry. Python standard library only."""
import base64
import concurrent.futures
import hashlib
import hmac
import http.client
import ipaddress
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import socket
import secrets
import struct
import threading
import time
import signal
import uuid
from storage import Store
from alerts import AlertEngine
from health import Health
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, parse_qs, unquote
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
INTERVAL = max(3, int(os.getenv('SIGNAL_INTERVAL', '5')))
DATA_DIR = Path(os.getenv('SIGNAL_DATA_DIR', str(ROOT / 'data')))
PROC_ROOT = Path(os.getenv('SIGNAL_PROC_ROOT', '/proc'))
SYS_ROOT = Path(os.getenv('SIGNAL_SYS_ROOT', '/sys'))
RUN_ROOT = Path(os.getenv('SIGNAL_RUN_ROOT', '/run'))
AUTH_FILE = Path(os.getenv('SIGNAL_AUTH_FILE', str(DATA_DIR / 'access.json')))
AUTH = json.loads(AUTH_FILE.read_text()) if AUTH_FILE.exists() else None
PUBLIC_MODE = os.getenv('SIGNAL_PUBLIC_MODE') == '1'
ATTEMPTS = {}
AUTH_LOCK = threading.Lock()
# Optional app control: SIGNAL never touches Docker for writes; it asks the separate runner over a Unix socket.
CONTROL_SOCKET = os.getenv('SIGNAL_CONTROL_SOCKET') or None
CONTROL_ALLOW = [ipaddress.ip_network(item.strip(), strict=False) for item in
                 os.getenv('SIGNAL_CONTROL_ALLOW', '100.64.0.0/10,127.0.0.1/32,::1/128,fd7a:115c:a1e0::/48').split(',') if item.strip()]
CONTROL_ACTIONS = ('start', 'stop', 'restart', 'check', 'update', 'rollback')
CONTROL_GRANT_SECONDS = 600
CONTROL_RATE_LIMIT = 20  # control requests per minute per client
CONTROL_ATTEMPTS = {}
CONTROL_RATE = {}
CONTROL_CACHE = {}
TUNNEL_HEADERS = ('Cf-Ray', 'Cf-Connecting-Ip', 'Cf-Visitor', 'Cf-Ipcountry')
# Enrolling an authenticator (setup_2fa.py) also allows control through the public address, with a code on every step-up.
CONTROL_2FA_FILE = Path(os.getenv('SIGNAL_CONTROL_2FA_FILE', str(DATA_DIR / 'control-2fa.json')))
CONTROL_2FA_LOCK = threading.Lock()
CONTROL_2FA_USED = {}
PUBLIC_STEP_UP_LIMIT = 10  # failed public step-ups per minute across every address
# Disk cleanup: a root daemon (deploy/pi-clean.py serve) that only scans and applies its own findings by id.
CLEAN_SOCKET = os.getenv('SIGNAL_CLEAN_SOCKET', '/run/pi-clean/control.sock') or None
CLEAN_RATE = {}
CLEAN_LOCK = threading.Lock()


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


class ControlError(Exception):
    def __init__(self, message, code='unavailable'):
        super().__init__(message)
        self.code = code


def control_call(request, timeout=8):
    """One newline-delimited JSON request to the runner. Only fixed ops and validated values are sent."""
    if not CONTROL_SOCKET:
        raise ControlError('App control is not configured.')
    return unix_call(CONTROL_SOCKET, request, timeout, 'The app runner')


def clean_call(request, timeout=120):
    """The same protocol to the root cleanup daemon. One request at a time: it runs them one at a time anyway."""
    if not CLEAN_SOCKET:
        raise ControlError('Disk cleanup is not configured.')
    if not CLEAN_LOCK.acquire(timeout=1):
        raise ControlError('Another cleanup request is running. Try again when it finishes.', 'conflict')
    try:
        return unix_call(CLEAN_SOCKET, request, timeout, 'The cleanup service')
    finally:
        CLEAN_LOCK.release()


def unix_call(path, request, timeout, name):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(path)
            sock.sendall(json.dumps(request).encode() + b'\n')
            line = sock.makefile('rb').readline(4 * 1024 * 1024)
        response = json.loads(line)
    except (OSError, ValueError) as exc:
        raise ControlError(name + ' is not reachable.') from exc
    if not isinstance(response, dict) or not response.get('ok'):
        raise ControlError(str((response or {}).get('error') or name + ' refused the request.')[:300], (response or {}).get('code', 'invalid'))
    return response


def control_available():
    """Cached reachability so /api/session does not hit the runner on every page load."""
    if not CONTROL_SOCKET:
        return False
    now = time.monotonic()
    cached = CONTROL_CACHE.get('available')
    if cached and now - cached[0] < 10:
        return cached[1]
    try:
        control_call({'op': 'list'}, timeout=3)
        result = True
    except ControlError:
        result = False
    CONTROL_CACHE['available'] = (now, result)
    return result


def make_control_grant(session_token, expiry=None, scope='control'):
    """A 10-minute step-up grant bound to one session token, signed with a separate HMAC domain.
    Grants minted with a second factor use their own scope, so a password-only grant never works publicly."""
    payload = str(expiry or int(time.time()) + CONTROL_GRANT_SECONDS) + '.' + secrets.token_hex(8)
    message = (scope + ':' + payload + ':' + session_token.rsplit('.', 1)[0]).encode()
    return payload + '.' + hmac.new(bytes.fromhex(AUTH['secret']), message, hashlib.sha256).hexdigest()


def valid_control_grant(grant, session_token, scope='control'):
    if not AUTH or not grant or not session_token:
        return False
    try:
        payload, signature = grant.rsplit('.', 1)
        message = (scope + ':' + payload + ':' + session_token.rsplit('.', 1)[0]).encode()
        expected = hmac.new(bytes.fromhex(AUTH['secret']), message, hashlib.sha256).hexdigest()
        return time.time() < int(payload.split('.')[0]) and hmac.compare_digest(signature, expected)
    except (ValueError, AttributeError):
        return False


def load_2fa():
    try:
        data = json.loads(CONTROL_2FA_FILE.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get('secret'), str) and data['secret'] else None


def totp(secret, counter):
    """RFC 6238 code (SHA-1, 6 digits, 30 s), what Google Authenticator and friends show."""
    key = base64.b32decode(secret.upper() + '=' * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack('>Q', counter), hashlib.sha1).digest()
    offset = digest[-1] & 15
    return '%06d' % ((struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7fffffff) % 1000000)


def verify_second_factor(code):
    """A current authenticator code (one step of clock drift either way, each code once) or an unused
    backup code, which is spent."""
    code = re.sub(r'[\s-]', '', str(code or ''))[:32].lower()
    with CONTROL_2FA_LOCK:
        data = load_2fa()
        if not data or not code:
            return False
        if len(code) == 6 and code.isdigit():
            now = int(time.time()) // 30
            for counter in (now - 1, now, now + 1):
                if counter > CONTROL_2FA_USED.get('counter', -1) and hmac.compare_digest(totp(data['secret'], counter), code):
                    CONTROL_2FA_USED['counter'] = counter
                    return True
            return False
        digest = hashlib.sha256(code.encode()).hexdigest()
        backup = [item for item in data.get('backup') or [] if isinstance(item, str)]
        if not any(hmac.compare_digest(digest, item) for item in backup):
            return False
        data['backup'] = [item for item in backup if item != digest]
        temporary = CONTROL_2FA_FILE.with_name(CONTROL_2FA_FILE.name + '.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream)
        temporary.replace(CONTROL_2FA_FILE)
        return True


def join_apps(apps, containers):
    """Registry entries from the runner joined with containers SIGNAL already observes (project label == app id)."""
    result = []
    for app in apps:
        items = sorted((c for c in containers if c.get('project') == app.get('id')), key=lambda c: c['name'])
        running = sum(c['state'] == 'running' for c in items)
        result.append({**app, 'containers': {'running': running, 'total': len(items), 'unhealthy': sum(c.get('health') == 'unhealthy' for c in items),
                                             'items': [{'name': c['name'], 'service': c.get('service', ''), 'state': c['state'], 'health': c.get('health')} for c in items]}})
    return result


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
        self.http_checks = self.store.metadata('httpChecks', [])
        self.check_results = {}
        self.last_check_time = 0
        self.boot_at = time.time()
        self.health = Health(self.store, PROC_ROOT, SYS_ROOT, RUN_ROOT, os.getenv('SIGNAL_DISK_PATH', '/'))
        self.last_deploy_check = 0

    @staticmethod
    def http_check(check, now):
        started = time.monotonic()
        result = dict(check)
        try:
            request = Request(check['url'], headers={'User-Agent': 'SIGNAL/2.1'}, method='GET')
            with urlopen(request, timeout=3) as response:
                status = response.status
            result.update(ok=200 <= status < 400, status=status, error=None)
        except HTTPError as exc:
            result.update(ok=False, status=exc.code, error='HTTP '+str(exc.code))
        except (URLError, OSError, ValueError) as exc:
            reason = getattr(exc, 'reason', exc)
            result.update(ok=False, status=None, error=str(reason)[:160])
        result['latency'] = round((time.monotonic()-started)*1000, 1)
        result['checkedAt'] = now
        result['message'] = ('HTTP '+str(result['status'])) if result.get('status') else (result.get('error') or 'Connection failed')
        return result

    def configured_checks(self, containers):
        with self.lock:
            checks = list(self.http_checks)
        known = {item['url'] for item in checks}
        for container in containers:
            url = container.get('checkUrl')
            if url and url not in known:
                checks.append({'id': 'label-'+hashlib.sha1((container['name']+url).encode()).hexdigest()[:12],
                               'service': container.get('project', 'standalone'), 'name': container['name'], 'url': url, 'source': 'label'})
                known.add(url)
        return checks

    def update_check(self, body):
        action = body.get('action', 'upsert')
        check_id = str(body.get('id') or uuid.uuid4().hex[:12])
        removed = False
        with self.lock:
            if action == 'delete':
                removed = any(item['id'] == check_id for item in self.http_checks)
                self.http_checks = [item for item in self.http_checks if item['id'] != check_id]
            else:
                url = str(body.get('url', '')).strip()
                parsed = urlparse(url)
                if len(url) > 2048 or parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
                    raise ValueError('Enter a valid HTTP or HTTPS URL without embedded credentials.')
                service = str(body.get('service', '')).strip()[:100]
                name = str(body.get('name', '')).strip()[:100]
                if not service or not name:
                    raise ValueError('Service and check name are required.')
                item = {'id': check_id, 'service': service, 'name': name, 'url': url, 'source': 'manual'}
                self.http_checks = [current for current in self.http_checks if current['id'] != check_id] + [item]
            self.store.set_metadata('httpChecks', self.http_checks)
            self.last_check_time = 0
            checks = list(self.http_checks)
        if removed:
            self.alert_engine.clear_condition('http:'+check_id, time.time())
        return checks

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
        result['checkUrl'] = labels.get('signal.check.url') or None
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
        health = self.health.sample(now)  # slow readers are cached by the health thread
        throttle = int(health['power']['raw'], 16) if health['power'] else None
        configured_checks = self.configured_checks(containers)
        if configured_checks and (not self.check_results or now-self.last_check_time >= 15):
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                checked = list(pool.map(lambda item: self.http_check(item, now), configured_checks))
            self.check_results = {item['id']: item for item in checked}
            self.last_check_time = now
        check_ids = {item['id'] for item in configured_checks}
        self.check_results = {key: value for key, value in self.check_results.items() if key in check_ids}
        checks = [self.check_results.get(item['id'], {**item, 'ok': None, 'status': None, 'latency': None, 'checkedAt': None, 'message': 'Waiting for the first check'}) for item in configured_checks]
        data = {'timestamp': now, 'interval': INTERVAL, 'host': {'name': os.getenv('SIGNAL_NODE_NAME') or read('/proc/sys/kernel/hostname', socket.gethostname()), 'alias': os.getenv('SIGNAL_NODE_NAME') or read('/proc/sys/kernel/hostname', socket.gethostname()), 'model': read('/proc/device-tree/model').replace('\0', '') or read('/sys/class/dmi/id/product_name', 'Linux host'), 'kernel': os.uname().release, 'arch': os.uname().machine, 'uptime': float(read('/proc/uptime', '0').split()[0]), 'cpu': usage[0] if usage else 0, 'cores': usage[1:], 'frequency': int(freq)/1000 if freq else None, 'load': os.getloadavg(), 'temperature': temp, 'throttle': throttle, 'memory': {'total': memory['MemTotal'], 'used': memory['MemTotal']-memory['MemAvailable'], 'available': memory['MemAvailable'], 'cache': memory.get('Cached', 0), 'swapTotal': memory.get('SwapTotal', 0), 'swapUsed': memory.get('SwapTotal', 0)-memory.get('SwapFree', 0)}, 'disk': {'total': disk.total, 'used': disk.used, 'free': disk.free}, 'network': interfaces}, 'containers': sorted(containers, key=lambda c: (c['state'] == 'running' and c['health'] != 'unhealthy', c['name'])), 'checks': checks, 'dockerVersion': docker_version, 'processes': self.process_sample(elapsed), 'errors': errors, 'startedAt': self.boot_at}
        data['health'] = health
        point = {'time': now, 'cpu': data['host']['cpu'], 'memory': data['host']['memory']['used']/memory['MemTotal']*100, 'temperature': data['host']['temperature'], 'rx': sum(i['rx'] for i in interfaces), 'tx': sum(i['tx'] for i in interfaces), 'disk': disk.used/max(1, disk.total)*100}
        self.store.add(point)
        self.alert_engine.evaluate(data)
        self.alert_engine.evaluate_health(health, now)
        self.deploy_alerts(now)
        data['alerts'] = self.store.alerts()
        with self.lock:
            self.history.append(point)
            data['history'] = list(self.history)
            data['events'] = list(self.events)
            self.snapshot = data
        self.last_cpu, self.last_net, self.last_time = cpu, network, mono

    def deploy_alerts(self, now):
        """Failed or rolled-back app jobs become incidents. Polls the runner at most every 30 s."""
        if not CONTROL_SOCKET or now - self.last_deploy_check < 30:
            return
        self.last_deploy_check = now
        try:
            apps = control_call({'op': 'list'}, timeout=3)['apps']
        except ControlError:
            return
        self.alert_engine.evaluate_deploys(apps, now)

    def run(self):
        self.health.start(self.stop)
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

    def cookie_value(self, name):
        try:
            item = SimpleCookie(self.headers.get('Cookie', '')).get(name)
            return item.value if item else ''
        except Exception:
            return ''

    def peer(self):
        address = ipaddress.ip_address(self.client_address[0].split('%')[0])
        return address.ipv4_mapped or address if address.version == 6 else address

    def via_tunnel(self):
        # cloudflared connects from loopback, so the socket peer alone cannot tell public traffic apart.
        # Cloudflare's edge always adds these headers and clients cannot strip them; treat their presence as public.
        return any(self.headers.get(name) is not None for name in TUNNEL_HEADERS)

    def control_network_allowed(self):
        if self.via_tunnel():
            # Public requests may control apps only once an authenticator is enrolled; every step-up then needs its code.
            return load_2fa() is not None
        try:
            peer = self.peer()
        except ValueError:
            return False
        return any(peer.version == network.version and peer in network for network in CONTROL_ALLOW)

    def control_state(self, available=None):
        available = control_available() if available is None else available
        allowed = bool(available and AUTH and self.authenticated() and self.control_network_allowed())
        granted = allowed and valid_control_grant(self.cookie_value('signal_control'), self.cookie_value('signal_session'), self.grant_scope())
        return {'available': bool(available), 'allowed': allowed, 'granted': granted, 'secondFactor': allowed and self.via_tunnel()}

    def grant_scope(self):
        return 'control-2fa' if self.via_tunnel() else 'control'

    def cookie(self, token, age):
        secure = '; Secure' if self.headers.get('X-Forwarded-Proto') == 'https' else ''
        self.cookie_header = 'signal_session=' + token + '; Path=/; HttpOnly; SameSite=Strict; Max-Age=' + str(age) + secure

    def request_json(self):
        length = int(self.headers.get('Content-Length', '0'))
        if length < 1 or length > 8192:
            raise ValueError('Invalid request.')
        value = json.loads(self.rfile.read(length))
        if not isinstance(value, dict):
            raise ValueError('Invalid request.')
        return value

    def do_POST(self):
        path = urlparse(self.path).path
        origin = self.headers.get('Origin')
        if origin and urlparse(origin).netloc != self.headers.get('Host'):
            if re.fullmatch(r'/api/apps/[a-z0-9-]{1,40}/[a-z]+|/api/jobs/[a-f0-9]{32}/cancel', path) and CONTROL_SOCKET:
                self.audit(path.split('/')[3][:40], path.rsplit('/', 1)[1][:12], 'refused', 'Cross-origin request.')
            elif path == '/api/cleanup/apply' and CLEAN_SOCKET:
                self.audit('pi-clean', 'cleanup', 'refused', 'Cross-origin request.')
            return self.json(403, {'error': 'Request origin is not allowed.'})
        match = re.fullmatch(r'/api/apps/([a-z0-9-]{1,40})/([a-z]+)', path)
        if match and match[2] in CONTROL_ACTIONS:
            return self.control_post(match[1], match[2])
        match = re.fullmatch(r'/api/jobs/([a-f0-9]{32})/cancel', path)
        if match:
            return self.control_post(None, 'cancel', match[1])
        if path == '/api/cleanup/apply':
            return self.cleanup_post()
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
            with self.server.monitor.lock:
                snapshot = self.server.monitor.snapshot
            if snapshot is not None:
                self.server.monitor.alert_engine.decorate(snapshot)
            alerts = self.server.monitor.store.alerts()
            with self.server.monitor.lock:
                snapshot = self.server.monitor.snapshot
                current = next((c for c in (snapshot or {}).get('containers', []) if c['id'] == match.group(1)), None)
                if snapshot is not None:
                    snapshot['alerts'] = alerts
                result = {'ok': True, 'name': container['name'], 'monitoringMuted': current.get('monitoringMuted', muted) if current else muted, 'monitoringMuteSource': current.get('monitoringMuteSource') if current else ('manual' if muted else None), 'alerts': alerts}
            return self.json(200, result)
        if path == '/api/checks':
            if not AUTH or not self.authenticated():
                return self.json(401, {'error': 'A private session is required.'})
            try:
                body = self.request_json()
                checks = self.server.monitor.update_check(body)
                return self.json(200, {'ok': True, 'checks': checks, 'alerts': self.server.monitor.store.alerts()})
            except (ValueError, json.JSONDecodeError) as exc:
                return self.json(400, {'error': str(exc) or 'Invalid request.'})
        if path == '/api/maintenance':
            if not AUTH or not self.authenticated():
                return self.json(401, {'error': 'A private session is required.'})
            try:
                body = self.request_json()
                service = str(body.get('service', '')).strip()[:100]
                until = body.get('until')
                if not service or (until is not None and not isinstance(until, (int, float))):
                    raise ValueError('Service and a valid maintenance end are required.')
            except (ValueError, json.JSONDecodeError) as exc:
                return self.json(400, {'error': str(exc) or 'Invalid request.'})
            now = time.time()
            with self.server.monitor.lock:
                snapshot = self.server.monitor.snapshot
                containers = list((snapshot or {}).get('containers', []))
                checks = list((snapshot or {}).get('checks', []))
            known = {c.get('project', 'standalone') for c in containers} | {c.get('service', 'standalone') for c in checks}
            if service not in known:
                return self.json(404, {'error': 'Service is no longer available.'})
            self.server.monitor.alert_engine.set_maintenance(service, until, containers, checks, now)
            alerts = self.server.monitor.store.alerts()
            with self.server.monitor.lock:
                snapshot = self.server.monitor.snapshot
            if snapshot is not None:
                snapshot['timestamp'] = max(snapshot['timestamp'], now)
                self.server.monitor.alert_engine.decorate(snapshot)
                with self.server.monitor.lock:
                    snapshot['alerts'] = alerts
                    result = {'ok': True, 'maintenance': snapshot['maintenance'], 'containers': snapshot['containers'], 'checks': snapshot.get('checks', []), 'alerts': alerts}
            else:
                result = {'ok': True, 'maintenance': self.server.monitor.alert_engine.active_maintenance(now), 'containers': [], 'checks': [], 'alerts': alerts}
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
            return self.json(200, {'authenticated': self.authenticated(), 'authEnabled': AUTH is not None, 'logs': AUTH is not None, 'control': self.control_state()})
        if path.startswith('/api/') and not self.authenticated():
            return self.json(401, {'error': 'Sign in to view system data.'})
        if path.startswith(('/api/apps', '/api/jobs/')):
            return self.control_get(path, parse_qs(parsed.query))
        if path == '/api/cleanup':
            return self.cleanup_get(parse_qs(parsed.query))
        if path == '/api/history':
            ranges = {'1h': 3600, '24h': 86400, '7d': 604800}
            value = parse_qs(parsed.query).get('range', ['24h'])[0]
            if value not in ranges:
                return self.json(400, {'error': 'Use range=1h, 24h or 7d.'})
            return self.json(200, self.server.monitor.store.history(ranges[value]))
        if path == '/api/brief':
            return self.json(200, self.server.monitor.store.daily_brief())
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

    def client_key(self):
        return 'tunnel:' + str(self.headers.get('Cf-Connecting-Ip', ''))[:64] if self.via_tunnel() else self.client_address[0]

    def audit(self, name, action, outcome, detail):
        """Every control request, allowed or refused, lands in the journal and the service log."""
        event = {'time': time.time(), 'name': name, 'state': action, 'health': None, 'kind': 'control', 'outcome': outcome,
                 'detail': detail, 'actor': self.client_key()}
        print('Control:', outcome, action, name, 'from', event['actor'], '-', detail, flush=True)
        monitor = getattr(self.server, 'monitor', None)
        # Only signed-in requests reach the bounded journal; anonymous noise stays in the service log.
        if monitor is not None and hasattr(monitor, 'events') and AUTH and self.authenticated():
            with monitor.lock:
                monitor.events.appendleft(event)

    @staticmethod
    def limited(table, key, limit, record=True):
        now = time.time()
        with AUTH_LOCK:
            for item in list(table):
                table[item] = [t for t in table[item] if now - t < 60]
                if not table[item]:
                    del table[item]
            recent = table.get(key, [])
            if len(recent) >= limit:
                return True
            if record:
                table[key] = recent + [now]
            return False

    def control_refuse(self, name, action, code, message, **extra):
        self.audit(name, action, 'refused', message)
        return self.json(code, {'error': message, **extra})

    def control_preflight(self, name, action):
        """Rate, session, network, same-origin JSON. Returns the request body, or None once a refusal was sent."""
        if self.limited(CONTROL_RATE, self.client_key(), CONTROL_RATE_LIMIT):
            print('Control: rate limited', action, name, 'from', self.client_key(), flush=True)
            self.json(429, {'error': 'Too many control requests. Try again in a minute.'})
            return None
        if not AUTH or not self.authenticated():
            self.control_refuse(name, action, 401, 'A private session is required.')
            return None
        if not self.control_network_allowed():
            self.control_refuse(name, action, 403, 'App control is available over Tailscale or on the host, not through the public address.')
            return None
        origin, fetch_site = self.headers.get('Origin') or '', self.headers.get('Sec-Fetch-Site') or ''
        same_origin = urlparse(origin).netloc == self.headers.get('Host') if origin else fetch_site == 'same-origin'
        if not same_origin or not (self.headers.get('Content-Type') or '').startswith('application/json'):
            self.control_refuse(name, action, 403, 'Same-origin JSON request required.')
            return None
        try:
            return self.request_json() if int(self.headers.get('Content-Length') or 0) else {}
        except (ValueError, json.JSONDecodeError):
            self.json(400, {'error': 'Invalid request.'})
            return None

    def control_step_up(self, name, action, body):
        """A valid grant cookie, or the password (plus an authenticator code through the public address), which
        mints one for 10 minutes. Returns False once a refusal was sent."""
        session, public = self.cookie_value('signal_session'), self.via_tunnel()
        if valid_control_grant(self.cookie_value('signal_control'), session, self.grant_scope()):
            return True
        def refuse(code, message):
            self.control_refuse(name, action, code, message, stepUp=True, secondFactor=public)
            return False
        password, code = body.get('password'), body.get('code')
        # Public addresses are cheap to rotate, so public failures also share one budget.
        if (self.limited(CONTROL_ATTEMPTS, self.client_key(), 5, record=False)
                or public and self.limited(CONTROL_ATTEMPTS, 'public', PUBLIC_STEP_UP_LIMIT, record=False)):
            self.control_refuse(name, action, 429, 'Too many password attempts. Try again in a minute.')
            return False
        if not isinstance(password, str) or not password or public and (not isinstance(code, str) or not code):
            return refuse(403, 'Confirm with your access password and authenticator code.' if public else 'Confirm with your access password.')
        def failed(message):
            self.limited(CONTROL_ATTEMPTS, self.client_key(), 5)
            if public:
                self.limited(CONTROL_ATTEMPTS, 'public', PUBLIC_STEP_UP_LIMIT)
            return refuse(403, message)
        digest = hashlib.pbkdf2_hmac('sha256', password[:1024].encode(), bytes.fromhex(AUTH['salt']), AUTH['iterations']).hex()
        if not hmac.compare_digest(digest, AUTH['hash']):
            return failed('Incorrect password.')
        if public and not verify_second_factor(code):
            return failed('Incorrect or already used authenticator code.')
        with AUTH_LOCK:
            CONTROL_ATTEMPTS.pop(self.client_key(), None)
        secure = '; Secure' if self.headers.get('X-Forwarded-Proto') == 'https' else ''
        self.cookie_header = ('signal_control=' + make_control_grant(session, scope=self.grant_scope()) + '; Path=/; HttpOnly; SameSite=Strict; Max-Age='
                              + str(CONTROL_GRANT_SECONDS) + secure)
        return True

    def control_post(self, app_id, action, job_id=None):
        """Gate order: configured, rate, session, network, same-origin, registry, step-up (password, plus an
        authenticator code through the public address). Only {app, action} reach the runner."""
        if not CONTROL_SOCKET:
            return self.json(404, {'error': 'Unknown endpoint.'})
        name = app_id or job_id[:12]
        body = self.control_preflight(name, action)
        if body is None:
            return None
        try:
            if app_id is not None and app_id not in {app.get('id') for app in control_call({'op': 'list'})['apps']}:
                return self.control_refuse(name, action, 404, 'Unknown app.')
            if action != 'check' and not self.control_step_up(name, action, body):
                return None
            if job_id:
                result = control_call({'op': 'cancel', 'id': job_id})
                self.audit(name, action, 'accepted', 'Cancel requested.')
                return self.json(200, result)
            result = control_call({'op': 'run', 'app': app_id, 'action': action, 'actor': 'session@' + self.client_key()})
        except ControlError as exc:
            code = {'not_found': 404, 'conflict': 409, 'unavailable': 503}.get(exc.code, 400)
            return self.control_refuse(name, action, code, str(exc))
        self.audit(name, action, 'accepted', 'Job ' + str(result['job'].get('id', ''))[:12] + ' queued.')
        return self.json(202, result)

    def cleanup_state(self):
        """Who may apply cleanup from here: the app-control network and step-up rules, without needing the runner."""
        return self.control_state(True)

    def cleanup_post(self):
        """Apply one cleanup finding by id: the app-control gate and step-up, then the root daemon re-checks the
        item against its own scan. The id is the only thing that leaves SIGNAL."""
        if not CLEAN_SOCKET:
            return self.json(404, {'error': 'Unknown endpoint.'})
        body = self.control_preflight('pi-clean', 'cleanup')
        if body is None:
            return None
        item = body.get('id')
        if not isinstance(item, str) or not 0 < len(item) <= 512:
            return self.json(400, {'error': 'Invalid request.'})
        if not self.control_step_up('pi-clean', 'cleanup', body):
            return None
        try:
            result = clean_call({'op': 'apply', 'id': item}, timeout=600)
        except ControlError as exc:
            code = {'not_found': 404, 'conflict': 409}.get(exc.code, 503)
            return self.control_refuse('pi-clean', 'cleanup', code, str(exc))
        result.pop('ok', None)
        outcome = 'Applied ' if result.get('applied') else 'Skipped '
        self.audit('pi-clean', 'cleanup', 'accepted', outcome + item[:120] + ', ' + str(result.get('freed', 0)) + ' bytes freed.')
        return self.json(200, result)

    def cleanup_get(self, query):
        if not CLEAN_SOCKET:
            return self.json(404, {'error': 'Unknown endpoint.'})
        refresh = query.get('refresh', ['0'])[0] == '1'
        if refresh and self.limited(CLEAN_RATE, self.client_key(), 6):
            return self.json(429, {'error': 'Too many rescans. Try again in a minute.'})
        try:
            result = clean_call({'op': 'scan', 'refresh': refresh})
        except ControlError as exc:
            return self.json(409 if exc.code == 'conflict' else 503, {'error': str(exc)})
        result.pop('ok', None)
        return self.json(200, {**result, 'control': self.cleanup_state()})

    def control_get(self, path, query):
        if not CONTROL_SOCKET:
            return self.json(404, {'error': 'Unknown endpoint.'})
        try:
            if path == '/api/apps':
                apps = control_call({'op': 'list'})['apps']
                CONTROL_CACHE['available'] = (time.monotonic(), True)
                with self.server.monitor.lock:
                    containers = list((self.server.monitor.snapshot or {}).get('containers', []))
                return self.json(200, {'apps': join_apps(apps, containers), 'control': self.control_state(True)})
            match = re.fullmatch(r'/api/apps/([a-z0-9-]{1,40})/jobs', path)
            if match:
                return self.json(200, control_call({'op': 'jobs', 'app': match[1], 'limit': 20}))
            match = re.fullmatch(r'/api/jobs/([a-f0-9]{32})(/log)?', path)
            if match and match[2]:
                if AUTH is None:
                    return self.json(403, {'error': 'Job logs require a private session.'})
                try:
                    offset = max(0, int(query.get('offset', ['0'])[0]))
                    request = {'op': 'log', 'id': match[1], 'offset': offset}
                    if 'epoch' in query:
                        request['epoch'] = int(query['epoch'][0])
                except ValueError:
                    return self.json(400, {'error': 'Invalid offset.'})
                return self.json(200, control_call(request))
            if match:
                return self.json(200, control_call({'op': 'job', 'id': match[1]}))
            return self.json(404, {'error': 'Unknown endpoint.'})
        except ControlError as exc:
            if exc.code == 'unavailable':
                CONTROL_CACHE['available'] = (time.monotonic(), False)
                return self.json(503, {'error': str(exc)})
            return self.json(404 if exc.code == 'not_found' else 409 if exc.code == 'conflict' else 400, {'error': str(exc)})

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
