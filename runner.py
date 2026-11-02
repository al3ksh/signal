"""SIGNAL runner: the only SIGNAL process allowed to change Compose apps. Python standard library only.

It listens on a Unix socket for newline-delimited JSON. A request names an app from the host registry
(/etc/signal/apps.json) and an operation from a fixed set; no request field ever becomes part of a
command line. Commands run as argv lists (shell=False) with cwd set to the app directory.
"""
import json
import os
from pathlib import Path
import re
import socketserver
import sqlite3
import subprocess
import threading
import time

REGISTRY = os.getenv('SIGNAL_RUNNER_REGISTRY', '/etc/signal/apps.json')
DATA_DIR = os.getenv('SIGNAL_RUNNER_DATA_DIR', '/var/lib/signal-runner')
SOCKET_PATH = os.getenv('SIGNAL_RUNNER_SOCKET', '/run/signal-runner/control.sock')
CHECK_INTERVAL = int(os.getenv('SIGNAL_RUNNER_CHECK_INTERVAL', '900'))
APP_ID = re.compile(r'[a-z0-9-]{1,40}')
REMOTE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,39}')
BRANCH = re.compile(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,99}')
GIT_TIMEOUT = 120
MAX_REQUEST = 64 * 1024


class RegistryError(ValueError):
    pass


class RequestError(Exception):
    def __init__(self, message, code='invalid'):
        super().__init__(message)
        self.code = code


def load_registry(path):
    """Validate the host registry. Any problem refuses the whole file: a half-loaded registry is worse than none."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise RegistryError(f'Cannot read registry {path}: {exc}')
    apps = data.get('apps') if isinstance(data, dict) else None
    if not isinstance(apps, list) or not apps:
        raise RegistryError('The registry needs a non-empty "apps" list.')
    result = {}
    for index, raw in enumerate(apps):
        where = f'apps[{index}]'
        if not isinstance(raw, dict):
            raise RegistryError(f'{where} must be an object.')
        app_id = raw.get('id')
        if not isinstance(app_id, str) or not APP_ID.fullmatch(app_id):
            raise RegistryError(f'{where}: id must match [a-z0-9-]{{1,40}} (the Compose project name).')
        if app_id in result:
            raise RegistryError(f'{where}: duplicate id {app_id}.')
        name = raw.get('name', app_id)
        if not isinstance(name, str) or not 0 < len(name) <= 80:
            raise RegistryError(f'{app_id}: name must be 1-80 characters.')
        directory = raw.get('dir')
        if not isinstance(directory, str) or not os.path.isabs(directory):
            raise RegistryError(f'{app_id}: dir must be an absolute path.')
        root = Path(directory).resolve()
        if not root.is_dir():
            raise RegistryError(f'{app_id}: dir {directory} does not exist.')
        files = raw.get('composeFiles')
        if not isinstance(files, list) or not 0 < len(files) <= 8:
            raise RegistryError(f'{app_id}: composeFiles must list 1-8 files.')
        compose = []
        for name_ in files:
            if not isinstance(name_, str) or not name_ or os.path.isabs(name_):
                raise RegistryError(f'{app_id}: compose files are relative paths inside dir.')
            resolved = (root / name_).resolve()
            if not resolved.is_relative_to(root) or not resolved.is_file():
                raise RegistryError(f'{app_id}: compose file {name_} must exist inside dir.')
            compose.append(str(resolved))
        repo = raw.get('repo')
        if repo is not None:
            if not isinstance(repo, dict):
                raise RegistryError(f'{app_id}: repo must be an object.')
            remote, branch = repo.get('remote', 'origin'), repo.get('branch', 'main')
            if not isinstance(remote, str) or not REMOTE.fullmatch(remote):
                raise RegistryError(f'{app_id}: invalid repo.remote.')
            if not isinstance(branch, str) or not BRANCH.fullmatch(branch) or '..' in branch or branch.endswith(('/', '.lock')):
                raise RegistryError(f'{app_id}: invalid repo.branch.')
            repo = {'remote': remote, 'branch': branch}
        health = raw.get('health')
        if health is not None:
            if not isinstance(health, dict):
                raise RegistryError(f'{app_id}: health must be an object.')
            url = health.get('url')
            if url is not None and (not isinstance(url, str) or not re.fullmatch(r'https?://[^\s@]{1,2000}', url)):
                raise RegistryError(f'{app_id}: health.url must be an http(s) URL without credentials.')
            expect, timeout = health.get('expect', 200), health.get('timeoutSeconds', 90)
            if not isinstance(expect, int) or not 100 <= expect <= 599:
                raise RegistryError(f'{app_id}: health.expect must be an HTTP status.')
            if not isinstance(timeout, int) or not 1 <= timeout <= 900:
                raise RegistryError(f'{app_id}: health.timeoutSeconds must be 1-900.')
            health = {'url': url, 'expect': expect, 'timeoutSeconds': timeout}
        update = raw.get('update') or {}
        mode = update.get('mode', 'build') if isinstance(update, dict) else None
        if mode not in ('build', 'pull'):
            raise RegistryError(f'{app_id}: update.mode must be "build" or "pull".')
        result[app_id] = {'id': app_id, 'name': name, 'dir': str(root), 'composeFiles': compose,
                          'repo': repo, 'health': health, 'mode': mode}
    return result


class JobStore:
    """Job history in SQLite, following storage.py: one connection, RLock, WAL."""
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY, app TEXT, action TEXT, actor TEXT, status TEXT, phase TEXT,
              created REAL, started REAL, finished REAL, exit_code INTEGER,
              prev_commit TEXT, new_commit TEXT, message TEXT, images TEXT);
            CREATE INDEX IF NOT EXISTS jobs_app ON jobs(app, created);
            CREATE TABLE IF NOT EXISTS checks (app TEXT PRIMARY KEY, value TEXT);
        ''')

    def check(self, app):
        with self.lock:
            row = self.db.execute('SELECT value FROM checks WHERE app=?', (app,)).fetchone()
            return json.loads(row[0]) if row else None

    def set_check(self, app, value):
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO checks VALUES (?,?)', (app, json.dumps(value)))

    def jobs(self, app, limit=20):
        with self.lock:
            return [public_job(r) for r in self.db.execute('SELECT * FROM jobs WHERE app=? ORDER BY created DESC LIMIT ?', (app, limit))]

    def close(self):
        with self.lock:
            self.db.close()


def public_job(row):
    if row is None:
        return None
    job = dict(row)
    job.pop('images', None)
    return {'id': job['id'], 'app': job['app'], 'action': job['action'], 'actor': job['actor'], 'status': job['status'],
            'phase': job['phase'], 'created': job['created'], 'started': job['started'], 'finished': job['finished'],
            'exitCode': job['exit_code'], 'prevCommit': job['prev_commit'], 'newCommit': job['new_commit'], 'message': job['message']}


class Runner:
    def __init__(self, apps, data_dir, path=None, check_interval=CHECK_INTERVAL):
        self.apps = apps
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = JobStore(self.data_dir / 'jobs.db')
        self.path = path or os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin')
        self.check_interval = check_interval
        self.app_locks = {app_id: threading.Lock() for app_id in apps}
        self.stop = threading.Event()
        self.checker = None

    # -- process execution -------------------------------------------------
    def env(self, extra=None):
        """A minimal environment: nothing from the requester, only what git/compose need."""
        env = {'PATH': self.path, 'LANG': 'C.UTF-8', 'GIT_TERMINAL_PROMPT': '0',
               'GIT_SSH_COMMAND': 'ssh -o BatchMode=yes'}
        for key in ('HOME', 'DOCKER_HOST', 'DOCKER_CONFIG', 'SSH_AUTH_SOCK', 'XDG_RUNTIME_DIR'):
            if os.environ.get(key):
                env[key] = os.environ[key]
        env.update(extra or {})
        return env

    def quiet(self, app, argv, timeout=GIT_TIMEOUT):
        """Run a short command and return (exit code, stdout). Used by checks, never logged to a job."""
        try:
            done = subprocess.run(argv, cwd=app['dir'], env=self.env(), capture_output=True, text=True,
                                  timeout=timeout, check=False, shell=False)
            return done.returncode, done.stdout.strip(), done.stderr.strip()
        except (OSError, subprocess.TimeoutExpired) as exc:
            return -1, '', str(exc)

    def git(self, app, *args, timeout=GIT_TIMEOUT):
        code, out, err = self.quiet(app, ['git', *args], timeout)
        if code != 0:
            raise RequestError('git ' + args[0] + ' failed: ' + (err or out)[-300:], 'failed')
        return out

    # -- git inspection ----------------------------------------------------
    def inspect(self, app_id, fetch=True):
        """Deployed commit and distance to the configured branch. Never changes the working tree."""
        app = self.apps[app_id]
        repo = app['repo']
        if not repo:
            return None
        upstream = repo['remote'] + '/' + repo['branch']
        result = {'checkedAt': time.time(), 'fetched': False, 'error': None, 'remoteBranch': upstream}
        try:
            if fetch:
                self.git(app, 'fetch', '--quiet', '--no-tags', repo['remote'], repo['branch'])
                result['fetched'] = True
            head = self.git(app, 'log', '-1', '--format=%H%x09%ct%x09%s', 'HEAD').split('\t', 2)
            result.update(head=head[0], headShort=head[0][:7], committedAt=int(head[1]), subject=head[2][:200])
            code, branch, _ = self.quiet(app, ['git', 'symbolic-ref', '--quiet', '--short', 'HEAD'])
            result['branch'] = branch if code == 0 else None
            result['dirty'] = bool(self.git(app, 'status', '--porcelain', '--untracked-files=no'))
            result['behind'] = int(self.git(app, 'rev-list', '--count', 'HEAD..' + upstream))
            result['ahead'] = int(self.git(app, 'rev-list', '--count', upstream + '..HEAD'))
            log = self.git(app, 'log', '--format=%h%x09%s%x09%an%x09%ct', 'HEAD..' + upstream, '-n', '20')
            result['commits'] = [dict(zip(('sha', 'subject', 'author', 'time'), line.split('\t', 3))) for line in log.splitlines() if line.count('\t') == 3]
            for commit in result['commits']:
                commit['subject'], commit['author'], commit['time'] = commit['subject'][:200], commit['author'][:80], int(commit['time'])
            result['expectedBranch'] = repo['branch']
        except (RequestError, ValueError, IndexError) as exc:
            previous = self.store.check(app_id) or {}
            result = {**previous, 'checkedAt': time.time(), 'error': str(exc)[:300]}
        self.store.set_check(app_id, result)
        return result

    def start_checker(self, initial_delay=30):
        def loop():
            if self.stop.wait(initial_delay):
                return
            while not self.stop.is_set():
                for app_id, app in self.apps.items():
                    lock = self.app_locks[app_id]
                    if app['repo'] and lock.acquire(blocking=False):
                        try:
                            self.inspect(app_id, fetch=True)
                        finally:
                            lock.release()
                if self.stop.wait(self.check_interval):
                    return
        self.checker = threading.Thread(target=loop, daemon=True, name='checker')
        self.checker.start()

    # -- protocol ----------------------------------------------------------
    def summary(self, app_id):
        app = self.apps[app_id]
        jobs = self.store.jobs(app_id, 1)
        return {'id': app_id, 'name': app['name'], 'hasRepo': bool(app['repo']), 'mode': app['mode'],
                'branch': app['repo']['branch'] if app['repo'] else None, 'hasHealthUrl': bool(app['health'] and app['health']['url']),
                'git': self.store.check(app_id) if app['repo'] else None, 'lastJob': jobs[0] if jobs else None}

    def app_id(self, request):
        app_id = request.get('app')
        if not isinstance(app_id, str) or app_id not in self.apps:
            raise RequestError('Unknown app.', 'not_found')
        return app_id

    def handle(self, request):
        if not isinstance(request, dict):
            raise RequestError('Request must be a JSON object.')
        op = request.get('op')
        if op == 'list':
            return {'apps': [self.summary(app_id) for app_id in self.apps]}
        if op == 'status':
            return {'app': self.summary(self.app_id(request))}
        raise RequestError('Unknown operation.')

    def close(self):
        self.stop.set()
        if self.checker:
            self.checker.join(timeout=5)
        self.store.close()


class SocketHandler(socketserver.StreamRequestHandler):
    def handle(self):
        line = self.rfile.readline(MAX_REQUEST + 1)
        try:
            if len(line) > MAX_REQUEST:
                raise RequestError('Request is too large.')
            try:
                request = json.loads(line)
            except ValueError:
                raise RequestError('Request is not JSON.')
            response = {'ok': True, **self.server.runner.handle(request)}
        except RequestError as exc:
            response = {'ok': False, 'error': str(exc), 'code': exc.code}
        except Exception as exc:  # keep serving; the web server only sees a generic error
            print('Runner error:', type(exc).__name__, exc, flush=True)
            response = {'ok': False, 'error': 'Runner error.', 'code': 'failed'}
        self.wfile.write(json.dumps(response).encode() + b'\n')


class SocketServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def serve(runner, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    old = os.umask(0o117)
    try:
        server = SocketServer(str(path), SocketHandler)
    finally:
        os.umask(old)
    os.chmod(path, 0o660)
    server.runner = runner
    return server


def main():
    try:
        apps = load_registry(REGISTRY)
    except RegistryError as exc:
        raise SystemExit('Refusing to start: ' + str(exc))
    runner = Runner(apps, DATA_DIR)
    runner.start_checker()
    server = serve(runner, SOCKET_PATH)
    print(f'SIGNAL runner managing {len(apps)} apps on {SOCKET_PATH}', flush=True)
    import signal
    def shutdown(signum, frame):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        runner.close()


if __name__ == '__main__':
    main()
