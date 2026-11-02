"""SIGNAL runner: the only SIGNAL process allowed to change Compose apps. Python standard library only.

It listens on a Unix socket for newline-delimited JSON. A request names an app from the host registry
(/etc/signal/apps.json) and an operation from a fixed set; no request field ever becomes part of a
command line. Commands run as argv lists (shell=False) with cwd set to the app directory.
"""
import grp
import json
import os
from pathlib import Path
import re
import shlex
import signal
import socket
import socketserver
import sqlite3
import subprocess
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import uuid

REGISTRY = os.getenv('SIGNAL_RUNNER_REGISTRY', '/etc/signal/apps.json')
DATA_DIR = os.getenv('SIGNAL_RUNNER_DATA_DIR', '/var/lib/signal-runner')
SOCKET_PATH = os.getenv('SIGNAL_RUNNER_SOCKET', '/run/signal-runner/control.sock')
CHECK_INTERVAL = int(os.getenv('SIGNAL_RUNNER_CHECK_INTERVAL', '900'))
SOCKET_GROUP = os.getenv('SIGNAL_RUNNER_SOCKET_GROUP') or None
DOCKER_PROXY = os.getenv('SIGNAL_RUNNER_DOCKER_PROXY') or None
DOCKER_SOCKET = os.getenv('DOCKER_SOCKET', '/var/run/docker.sock')
# Exactly the Docker Engine reads SIGNAL's web server makes, nothing else.
DOCKER_READ = re.compile(r'/(?:v1\.\d{2}/)?(?:_ping|version|containers/json|containers/[a-f0-9]{12,64}/(?:json|stats|logs))(?:\?[A-Za-z0-9=&_.-]{0,300})?')
APP_ID = re.compile(r'[a-z0-9-]{1,40}')
REMOTE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,39}')
BRANCH = re.compile(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,99}')
GIT_TIMEOUT = 120
MAX_REQUEST = 64 * 1024
ACTIONS = ('start', 'stop', 'restart', 'check', 'update', 'rollback')
REPO_ACTIONS = ('check', 'update', 'rollback')
TIMEOUTS = {'lifecycle': 120, 'build': 1800, 'git': GIT_TIMEOUT}
LOG_CAP = 2 * 1024 * 1024
KEEP_JOBS = 200
JOB_ID = re.compile(r'[a-f0-9]{32}')
IMAGE_REF = re.compile(r'[a-z0-9][a-z0-9._/:-]{0,250}')
SERVICE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,62}')


class RegistryError(ValueError):
    pass


class StepFailed(Exception):
    def __init__(self, message, exit_code=None):
        super().__init__(message)
        self.exit_code = exit_code


class JobCancelled(Exception):
    pass


class RollbackDone(Exception):
    """The deploy failed and the previous commit is running again."""


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

    def create(self, job):
        with self.lock, self.db:
            self.db.execute('INSERT INTO jobs(id,app,action,actor,status,created) VALUES (?,?,?,?,?,?)',
                            (job['id'], job['app'], job['action'], job['actor'], 'queued', job['created']))

    def update(self, job_id, **fields):
        columns = ('status', 'phase', 'started', 'finished', 'exit_code', 'prev_commit', 'new_commit', 'message', 'images')
        assert set(fields) <= set(columns)
        with self.lock, self.db:
            self.db.execute('UPDATE jobs SET ' + ','.join(k + '=?' for k in fields) + ' WHERE id=?', (*fields.values(), job_id))

    def row(self, job_id):
        with self.lock:
            return self.db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()

    def job(self, job_id):
        return public_job(self.row(job_id))

    def recover(self, now):
        """Mark jobs a previous runner left unfinished as failed; return them so moved checkouts can be restored."""
        with self.lock, self.db:
            rows = [dict(r) for r in self.db.execute("SELECT * FROM jobs WHERE status IN ('queued','running')")]
            self.db.execute("UPDATE jobs SET status='failed', finished=?, message='Runner restarted; job interrupted.' WHERE status IN ('queued','running')", (now,))
            return rows

    def prune(self, keep):
        with self.lock, self.db:
            old = [r[0] for r in self.db.execute('SELECT id FROM jobs ORDER BY created DESC LIMIT -1 OFFSET ?', (keep,))]
            self.db.executemany('DELETE FROM jobs WHERE id=?', [(i,) for i in old])
            return old

    def last_update(self, app, status=None):
        with self.lock:
            query = "SELECT * FROM jobs WHERE app=? AND action='update'" + (' AND status=? AND prev_commit IS NOT NULL' if status else '') + ' ORDER BY created DESC LIMIT 1'
            return self.db.execute(query, (app, status) if status else (app,)).fetchone()

    def last_deploy(self, app):
        with self.lock:
            return public_job(self.db.execute("SELECT * FROM jobs WHERE app=? AND action != 'check' ORDER BY created DESC LIMIT 1", (app,)).fetchone())

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
        interrupted = self.store.recover(time.time())
        self.logs = self.data_dir / 'logs'
        self.logs.mkdir(exist_ok=True)
        self.timeouts = dict(TIMEOUTS)
        self.log_cap = LOG_CAP
        self.keep_jobs = KEEP_JOBS
        self.active = {}
        self.active_lock = threading.Lock()
        self.build_slot = threading.Semaphore(1)  # one build or pull at a time protects the Pi's RAM
        self.health_poll = 2.0
        self.path = path or os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin')
        self.check_interval = check_interval
        self.app_locks = {app_id: threading.Lock() for app_id in apps}
        self.stop = threading.Event()
        self.checker = None
        self.recover_interrupted(interrupted)

    # -- process execution -------------------------------------------------
    def env(self, extra=None):
        """A minimal environment: nothing from the requester, only what git/compose need."""
        env = {'PATH': self.path, 'LANG': 'C.UTF-8', 'GIT_TERMINAL_PROMPT': '0', 'GIT_SSH_COMMAND': 'ssh -o BatchMode=yes',
               'NO_COLOR': '1', 'COMPOSE_ANSI': 'never', 'BUILDKIT_PROGRESS': 'plain', 'COMPOSE_PROGRESS': 'plain'}
        for key in ('HOME', 'DOCKER_HOST', 'DOCKER_CONFIG', 'SSH_AUTH_SOCK', 'XDG_RUNTIME_DIR'):
            if os.environ.get(key):
                env[key] = os.environ[key]
        env.update(extra or {})
        return env

    def quiet(self, app, argv, timeout=GIT_TIMEOUT, env=None):
        """Run a short command and return (exit code, stdout, stderr). Used by checks, never logged to a job."""
        try:
            done = subprocess.run(argv, cwd=app['dir'], env=self.env(env), capture_output=True, text=True,
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

    # -- jobs --------------------------------------------------------------
    def recover_interrupted(self, rows):
        """A deploy killed after the checkout moved (runner stopped, host rebooted) is rolled back to the
        commit it started from, but only while the checkout is still exactly where that job left it."""
        for row in rows:
            app = self.apps.get(row['app'])
            prev, new = row.get('prev_commit'), row.get('new_commit')
            if (row['action'] not in ('update', 'rollback', 'recover') or not app or not app['repo'] or not prev or not new
                    or prev == new or row.get('phase') not in ('build', 'pull', 'up', 'health', 'rollback')):
                continue
            head = self.head(app)
            if head not in (prev, new):
                print(f"Not recovering {row['app']}: checkout is at {(head or '?')[:7]}, not where job {row['id'][:12]} left it.", flush=True)
                continue
            job = self.submit(app['id'], 'recover', 'runner (recovery)', source=row)
            self.store.update(row['id'], message=f"Runner restarted during {row['phase']}; recovery job {job['id'][:12]} restores {prev[:7]}.")

    def do_recover(self, handle, app):
        row = handle['source']
        prev, head = row['prev_commit'], self.head(app)
        self.write(handle, f"# {row['action']} job {row['id'][:12]} was interrupted during {row['phase']}\n")
        self.store.update(handle['id'], prev_commit=head, new_commit=prev)
        branch = self.jgit(handle, app, 'symbolic-ref', '--quiet', '--short', 'HEAD', check=False)
        if branch != app['repo']['branch']:
            raise StepFailed(f"Checked out {branch or 'a detached HEAD'}, but the registry expects {app['repo']['branch']}. Not recovering automatically.")
        self.restore(handle, app, prev, json.loads(row.get('images') or '[]'), f"Recovering from an interrupted {row['action']}")

    def submit(self, app_id, action, actor, source=None):
        if app_id not in self.apps:
            raise RequestError('Unknown app.', 'not_found')
        if action not in ACTIONS and not (action == 'recover' and source):
            raise RequestError('Unknown action.')
        if action in REPO_ACTIONS and not self.apps[app_id]['repo']:
            raise RequestError('This app has no repository configured.')
        actor = re.sub(r'[^\x20-\x7e]', '_', str(actor or 'unknown'))[:80]
        with self.active_lock:
            if any(h['app'] == app_id and h['action'] == action for h in self.active.values()):
                raise RequestError(f'A {action} job for this app is already queued or running.', 'conflict')
            if len(self.active) >= 20:
                raise RequestError('Too many queued jobs.', 'conflict')
            handle = {'id': uuid.uuid4().hex, 'app': app_id, 'action': action, 'actor': actor, 'created': time.time(),
                      'cancelled': threading.Event(), 'proc': None, 'timed_out': False, 'log': None, 'size': 0,
                      'head': b'', 'total': 0, 'epoch': 0, 'source': source}
            self.store.create(handle)
            self.active[handle['id']] = handle
        threading.Thread(target=self.execute, args=(handle,), daemon=True, name='job-' + handle['id'][:8]).start()
        return self.store.job(handle['id'])

    def execute(self, handle):
        app = self.apps[handle['app']]
        status, message, exit_code = 'failed', None, None
        try:
            with self.app_locks[app['id']], open(self.logs / (handle['id'] + '.log'), 'w+b') as log:
                handle['log'] = log
                if handle['cancelled'].is_set():
                    raise JobCancelled()
                self.store.update(handle['id'], status='running', started=time.time())
                self.write(handle, f"# {handle['action']} {app['id']} by {handle['actor']} at {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                try:
                    message = getattr(self, 'do_' + handle['action'])(handle, app)
                    status = 'succeeded'
                except RollbackDone as exc:
                    status, message = 'rolled_back', str(exc)
                except JobCancelled:
                    status, message = 'cancelled', 'Cancelled.'
                except StepFailed as exc:
                    message, exit_code = str(exc), exc.exit_code
                except Exception as exc:
                    message = 'Runner error: ' + type(exc).__name__
                    print('Job error:', handle['id'], type(exc).__name__, exc, flush=True)
                self.write(handle, f'# {status}' + (f': {message}' if message else '') + '\n')
        except JobCancelled:
            status, message = 'cancelled', 'Cancelled before it started.'
        finally:
            handle['log'] = None
            try:
                if app['repo'] and handle['action'] != 'check':
                    self.inspect(app['id'], fetch=False)
            finally:
                self.store.update(handle['id'], status=status, message=(message or None) and str(message)[:500], exit_code=exit_code, finished=time.time())
                for old in self.store.prune(self.keep_jobs):
                    (self.logs / (old + '.log')).unlink(missing_ok=True)
                with self.active_lock:
                    self.active.pop(handle['id'], None)

    def write(self, handle, text):
        """Append to the job log. Past log_cap the file keeps the first quarter and the most recent half,
        because the end of a failed build is what explains it."""
        log = handle['log']
        if log is None:
            return
        data = text.encode('utf-8', 'replace')
        handle['total'] += len(data)
        room = self.log_cap // 4 - len(handle['head'])
        if room > 0:
            handle['head'] += data[:room]
        log.write(data)
        handle['size'] += len(data)
        if handle['size'] > self.log_cap:
            self.compact(handle)
        log.flush()

    def compact(self, handle):
        log = handle['log']
        log.flush()
        log.seek(0)
        tail = log.read()[-(self.log_cap // 2):]
        newline = tail.find(b'\n')
        tail = tail[newline + 1:] if 0 <= newline < 4096 else tail
        head = handle['head']
        head = head[:head.rfind(b'\n') + 1] or head
        marker = b'\n[... %d bytes of output omitted; the log keeps the beginning and the most recent output ...]\n\n' % max(0, handle['total'] - len(head) - len(tail))
        content = head + marker + tail
        log.seek(0)
        log.truncate()
        log.write(content)
        handle['size'] = len(content)
        handle['epoch'] += 1

    def kill(self, handle, proc, reason):
        if reason == 'timeout':
            handle['timed_out'] = True
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass

    def step(self, handle, app, argv, timeout, env=None, check=True, capture=False, cancellable=True):
        """Run one logged command for a job. argv only ever holds registry values and fixed words."""
        if cancellable and handle['cancelled'].is_set():
            raise JobCancelled()
        self.write(handle, '$ ' + ' '.join(shlex.quote(part) for part in argv) + '\n')
        try:
            proc = subprocess.Popen(argv, cwd=app['dir'], env=self.env(env), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, start_new_session=True, shell=False)
        except OSError as exc:
            raise StepFailed(f'{argv[0]} could not start: {exc}')
        handle['proc'], handle['timed_out'] = (proc if cancellable else None), False
        timer = threading.Timer(timeout, self.kill, (handle, proc, 'timeout'))
        timer.daemon = True
        timer.start()
        output, kept = [], 0
        try:
            for raw in iter(proc.stdout.readline, b''):
                line = re.sub(r'\x1b\[[0-9;?]*[A-Za-z]', '', raw.decode('utf-8', 'replace'))
                self.write(handle, line)
                if capture and kept < 1024 * 1024:
                    output.append(line)
                    kept += len(line)
            code = proc.wait()
        finally:
            timer.cancel()
            handle['proc'] = None
            proc.stdout.close()
        prefix = len(self.compose(app))
        label = 'docker compose ' + argv[prefix] if argv[:2] == ['docker', 'compose'] and len(argv) > prefix else ' '.join(argv[:2])
        if cancellable and handle['cancelled'].is_set():
            raise JobCancelled()
        if handle['timed_out']:
            raise StepFailed(f'{label} timed out after {timeout:g} s')
        if code != 0 and check:
            raise StepFailed(f'{label} exited with status {code}', code)
        return code, ''.join(output)

    def compose(self, app, *args):
        argv = ['docker', 'compose', '--project-directory', app['dir'], '-p', app['id']]
        for path in app['composeFiles']:
            argv += ['-f', path]
        return argv + list(args)

    def do_start(self, handle, app):
        self.step(handle, app, self.compose(app, 'up', '-d'), self.timeouts['lifecycle'])

    def do_stop(self, handle, app):
        self.step(handle, app, self.compose(app, 'stop'), self.timeouts['lifecycle'])

    def do_restart(self, handle, app):
        self.step(handle, app, self.compose(app, 'restart'), self.timeouts['lifecycle'])

    def do_check(self, handle, app):
        self.write(handle, f"$ git fetch {app['repo']['remote']} {app['repo']['branch']} (and compare)\n")
        result = self.inspect(app['id'], fetch=True)
        if result.get('error'):
            raise StepFailed(result['error'])
        self.write(handle, f"HEAD {result['headShort']} on {result.get('branch') or 'detached HEAD'}; {result['behind']} commit(s) behind, "
                           f"{result['ahead']} ahead of {result['remoteBranch']}; working tree {'dirty' if result['dirty'] else 'clean'}.\n")
        for commit in result['commits']:
            self.write(handle, f"  {commit['sha']} {commit['subject']} ({commit['author']})\n")
        return f"{result['behind']} commit(s) behind"

    # -- update and rollback ----------------------------------------------
    def phase(self, handle, name):
        self.store.update(handle['id'], phase=name)
        self.write(handle, f'# phase: {name}\n')

    def jgit(self, handle, app, *args, check=True, cancellable=True):
        code, out = self.step(handle, app, ['git', *args], self.timeouts['git'], check=check, capture=True, cancellable=cancellable)
        return out.strip() if code == 0 else None

    def preflight(self, handle, app):
        """Only a clean checkout of the configured branch may move."""
        branch = self.jgit(handle, app, 'symbolic-ref', '--quiet', '--short', 'HEAD', check=False)
        if branch != app['repo']['branch']:
            raise StepFailed(f"Checked out {branch or 'a detached HEAD'}, but the registry expects {app['repo']['branch']}. Refusing to deploy.")
        if self.jgit(handle, app, 'status', '--porcelain', '--untracked-files=no'):
            raise StepFailed('The working tree has local changes. Refusing to update; commit, stash or discard them on the host first.')

    @staticmethod
    def commit_env(commit):
        # Compose files may pin CI images with ${SIGNAL_COMMIT:-main}; harmless when unused.
        return {'SIGNAL_COMMIT': commit} if commit else {}

    def head(self, app):
        code, out, _ = self.quiet(app, ['git', 'rev-parse', 'HEAD'])
        return out if code == 0 else None

    def save_images(self, handle, app, commit):
        """Tag the images of the running commit as <name>:signal-prev so a rollback needs no rebuild or pull."""
        code, out = self.step(handle, app, self.compose(app, 'config', '--images'), 60, env=self.commit_env(commit), check=False, capture=True)
        pairs, names = [], set()
        for image in sorted(set(out.split())) if code == 0 else []:
            if '@' in image or not IMAGE_REF.fullmatch(image):
                continue
            last = image.rsplit('/', 1)[-1]
            name = image.rsplit(':', 1)[0] if ':' in last else image
            if name in names:
                continue
            names.add(name)
            if self.step(handle, app, ['docker', 'image', 'tag', image, name + ':signal-prev'], 60, check=False)[0] == 0:
                pairs.append([image, name + ':signal-prev'])
        return pairs

    def restore_images(self, handle, app, pairs):
        if not pairs:
            return False
        ok = True
        for image, backup in pairs:
            ok = self.step(handle, app, ['docker', 'image', 'tag', backup, image], 60, check=False, cancellable=False)[0] == 0 and ok
        return ok

    def changed_services(self, handle, app, prev, new):
        """Services whose build context or Dockerfile changed between prev and new; None means build everything."""
        diff = self.jgit(handle, app, 'diff', '--name-only', prev, new, check=False)
        top = self.jgit(handle, app, 'rev-parse', '--show-toplevel', check=False)
        if diff is None or not top:
            return None
        top = Path(top).resolve()
        changed = [line for line in diff.splitlines() if line]
        relative = lambda path: str(path.relative_to(top)) if path.is_relative_to(top) else None
        if any(relative(Path(f)) in changed for f in app['composeFiles']):
            self.write(handle, '# a Compose file changed; building every service\n')
            return None
        # Not logged: the rendered config contains interpolated .env values.
        code, out, _ = self.quiet(app, self.compose(app, 'config', '--format', 'json'), 60, env=self.commit_env(new))
        try:
            services = json.loads(out)['services'] if code == 0 else None
            assert isinstance(services, dict)
        except (ValueError, KeyError, TypeError, AssertionError):
            return None
        selected, buildable = [], 0
        for name, config in services.items():
            build = config.get('build') if isinstance(config, dict) else None
            if not build:
                continue
            if not SERVICE.fullmatch(name):
                return None
            buildable += 1
            build = {'context': build} if isinstance(build, str) else build
            context = str(build.get('context') or '.')
            if '://' in context or context.startswith('git@'):
                selected.append(name)
                continue
            context = Path(context if os.path.isabs(context) else os.path.join(app['dir'], context)).resolve()
            dockerfile = str(build.get('dockerfile') or 'Dockerfile')
            dockerfile = Path(dockerfile if os.path.isabs(dockerfile) else context / dockerfile).resolve()
            ctx, df = relative(context), relative(dockerfile)
            if ctx is None or df is None or any(path == df or ctx == '.' or path.startswith(ctx + '/') for path in changed):
                selected.append(name)
        if not buildable:
            return None
        self.write(handle, f"# build contexts changed: {', '.join(selected) or 'none'}\n")
        return selected

    def build(self, handle, app, commit, cancellable=True, services=None):
        """Build on the host or pull CI images, one at a time across all apps."""
        if not self.build_slot.acquire(blocking=False):
            self.write(handle, '# waiting for another build to finish\n')
            while not self.build_slot.acquire(timeout=0.5):
                if cancellable and handle['cancelled'].is_set():
                    raise JobCancelled()
        try:
            args = ('pull',) if app['mode'] == 'pull' else ('build', '--pull', *(services or ()))
            self.step(handle, app, self.compose(app, *args), self.timeouts['build'], env=self.commit_env(commit), cancellable=cancellable)
        finally:
            self.build_slot.release()

    def health_problem(self, app, commit):
        code, out, err = self.quiet(app, self.compose(app, 'ps', '-a', '--format', 'json'), 60, env=self.commit_env(commit))
        if code != 0:
            return 'docker compose ps failed: ' + (err or out)[-200:]
        try:
            text = out.strip()
            rows = json.loads(text) if text.startswith('[') else [json.loads(line) for line in text.splitlines() if line.strip()]
        except ValueError:
            return 'docker compose ps returned unreadable output'
        if not rows:
            return 'no containers exist'
        for row in rows:
            state, health = str(row.get('State', '')).lower(), str(row.get('Health', '')).lower()
            if state == 'exited' and row.get('ExitCode') == 0:
                continue  # finished one-shot service (migrations, init)
            if state != 'running' or health in ('unhealthy', 'starting'):
                return f"{row.get('Service') or row.get('Name') or 'a container'} is {state}{'/' + health if health else ''}"
        url = (app['health'] or {}).get('url')
        if url:
            expect = app['health']['expect']
            try:
                with urlopen(Request(url, headers={'User-Agent': 'SIGNAL-runner'}), timeout=5) as response:
                    status = response.status
            except HTTPError as exc:
                status = exc.code
            except (URLError, OSError, ValueError) as exc:
                return f'{url} is unreachable ({getattr(exc, "reason", exc)})'
            if status != expect:
                return f'{url} answered HTTP {status}, expected {expect}'
        return None

    def health(self, handle, app, commit, cancellable=True):
        timeout = (app['health'] or {}).get('timeoutSeconds', 60)
        url = (app['health'] or {}).get('url')
        self.write(handle, f"# waiting up to {timeout} s for running containers{' and ' + url if url else ''}\n")
        deadline, problem = time.monotonic() + timeout, None
        while True:
            if cancellable and handle['cancelled'].is_set():
                raise JobCancelled()
            problem = self.health_problem(app, commit)
            if problem is None:
                self.write(handle, '# healthy\n')
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise StepFailed(f'Health check failed after {timeout} s: {problem}')
            time.sleep(min(self.health_poll, remaining))

    def ensure_published(self, handle, app, prev, target):
        """Before moving the checkout, make sure CI already pushed the images pinned to the target commit
        (those whose reference changes with SIGNAL_COMMIT). Otherwise the pull would fail and roll back."""
        def images(commit):
            code, out, _ = self.quiet(app, self.compose(app, 'config', '--images'), 60, env=self.commit_env(commit))
            return set(out.split()) if code == 0 else None
        before, after = images(prev), images(target)
        if before is None or after is None:
            return
        for image in sorted(after - before):
            if '@' in image or not IMAGE_REF.fullmatch(image):
                continue
            code, _, err = self.quiet(app, ['docker', 'manifest', 'inspect', image], 60)
            self.write(handle, f"$ docker manifest inspect {image}: {'published' if code == 0 else 'not found'}\n")
            if code != 0:
                raise StepFailed(f'{image} is not published yet. Wait for the CI build of {target[:7]} to finish, then update again.'
                                 + (f' ({err[-160:]})' if err and 'no such manifest' not in err.lower() else ''))

    def deploy(self, handle, app, prev, move, artifacts):
        """Move the checkout, produce images, start and verify. Any failure after the move restores prev."""
        images = self.save_images(handle, app, prev)
        self.store.update(handle['id'], prev_commit=prev, images=json.dumps(images))
        move()
        new = self.head(app)
        self.store.update(handle['id'], new_commit=new)
        try:
            artifacts(new)
            self.phase(handle, 'up')
            self.step(handle, app, self.compose(app, 'up', '-d', '--remove-orphans'), self.timeouts['lifecycle'], env=self.commit_env(new))
            self.phase(handle, 'health')
            self.health(handle, app, new)
        except (StepFailed, JobCancelled) as exc:
            self.restore(handle, app, prev, images, 'Cancelled' if isinstance(exc, JobCancelled) else str(exc))
        self.phase(handle, 'done')
        return new

    def restore(self, handle, app, prev, images, reason):
        self.phase(handle, 'rollback')
        self.write(handle, f'# {reason}\n# rolling back to {prev[:12]}\n')
        try:
            self.step(handle, app, ['git', 'reset', '--hard', prev], self.timeouts['git'], cancellable=False)
            if not self.restore_images(handle, app, images) and app['mode'] == 'build':
                self.build(handle, app, prev, cancellable=False)
            self.step(handle, app, self.compose(app, 'up', '-d', '--remove-orphans'), self.timeouts['lifecycle'], env=self.commit_env(prev), cancellable=False)
            self.health(handle, app, prev, cancellable=False)
        except StepFailed as exc:
            raise StepFailed(f'{reason}. The rollback to {prev[:7]} also failed: {exc}')
        self.phase(handle, 'rolled back')
        raise RollbackDone(f'{reason}. Rolled back to {prev[:7]}.')

    def do_update(self, handle, app):
        repo = app['repo']
        upstream = repo['remote'] + '/' + repo['branch']
        self.phase(handle, 'fetch')
        self.jgit(handle, app, 'fetch', '--no-tags', repo['remote'], repo['branch'])
        self.preflight(handle, app)
        prev = self.jgit(handle, app, 'rev-parse', 'HEAD')
        target = self.jgit(handle, app, 'rev-parse', upstream)
        if int(self.jgit(handle, app, 'rev-list', '--count', upstream + '..HEAD')):
            raise StepFailed(f'The checkout has diverged from {upstream} (local commits not on the remote). Only fast-forward updates are allowed.')
        if prev == target:
            self.phase(handle, 'done')
            return f'Already up to date with {upstream} at {prev[:7]}.'
        if app['mode'] == 'pull':
            self.ensure_published(handle, app, prev, target)
        def artifacts(commit):
            self.phase(handle, 'pull' if app['mode'] == 'pull' else 'build')
            services = self.changed_services(handle, app, prev, commit) if app['mode'] == 'build' else None
            if services == []:
                self.write(handle, '# no build context changed; skipping the build\n')
                return
            self.build(handle, app, commit, services=services)
        new = self.deploy(handle, app, prev, lambda: self.jgit(handle, app, 'merge', '--ff-only', upstream), artifacts)
        return f'Deployed {new[:7]} (was {prev[:7]}).'

    def do_rollback(self, handle, app):
        row = self.store.last_update(app['id'], 'succeeded')
        if row is None:
            raise StepFailed('No successful update to roll back.')
        target = row['prev_commit']
        self.phase(handle, 'fetch')
        self.preflight(handle, app)
        prev = self.jgit(handle, app, 'rev-parse', 'HEAD')
        if prev == target:
            self.phase(handle, 'done')
            return f'Already at {target[:7]}.'
        if self.jgit(handle, app, 'cat-file', '-e', target + '^{commit}', check=False) is None:
            raise StepFailed(f'Commit {target[:7]} is no longer in the repository.')
        latest = self.store.last_update(app['id'])
        saved = json.loads(row['images'] or '[]') if latest is not None and latest['id'] == row['id'] else []
        def artifacts(commit):
            # The images of the commit before the last update are still tagged :signal-prev unless a later update replaced them.
            if self.restore_images(handle, app, saved):
                return
            if app['mode'] == 'build':
                self.phase(handle, 'build')
                self.build(handle, app, commit)
        self.deploy(handle, app, prev, lambda: self.jgit(handle, app, 'reset', '--hard', target), artifacts)
        return f'Rolled back to {target[:7]} (was {prev[:7]}).'

    def cancel(self, job_id):
        with self.active_lock:
            handle = self.active.get(job_id)
        if not handle:
            raise RequestError('Job is not running.', 'conflict' if self.store.row(job_id) else 'not_found')
        handle['cancelled'].set()
        proc = handle['proc']
        if proc:
            threading.Thread(target=self.kill, args=(handle, proc, 'cancel'), daemon=True).start()
        return self.store.job(job_id)

    def read_log(self, job_id, offset, limit=64 * 1024, epoch=None):
        """Incremental read. epoch changes whenever a live log is compacted (and is -1 once the job is over);
        a reader holding another epoch starts again from 0 and replaces what it shows."""
        row = self.store.row(job_id)
        if row is None:
            raise RequestError('Unknown job.', 'not_found')
        with self.active_lock:
            handle = self.active.get(job_id)
            current = handle['epoch'] if handle else -1
        offset = max(0, offset)
        reset = epoch is not None and epoch != current and offset > 0
        if reset:
            offset = 0
        path = self.logs / (job_id + '.log')
        data = b''
        if path.exists():
            with open(path, 'rb') as file:
                file.seek(offset)
                data = file.read(limit)
        return {'data': data.decode('utf-8', 'replace'), 'offset': offset + len(data), 'done': handle is None,
                'status': row['status'], 'epoch': current, 'reset': reset}

    # -- protocol ----------------------------------------------------------
    def summary(self, app_id):
        app = self.apps[app_id]
        jobs = self.store.jobs(app_id, 1)
        with self.active_lock:
            active = [h['id'] for h in self.active.values() if h['app'] == app_id]
        pending = [j for j in (self.store.job(i) for i in active) if j and j['status'] in ('queued', 'running')]
        active_job = next((j for j in pending if j['status'] == 'running'), pending[0] if pending else None)
        rollback = self.store.last_update(app_id, 'succeeded') if app['repo'] else None
        return {'rollbackTo': rollback['prev_commit'][:7] if rollback else None,'id': app_id, 'name': app['name'], 'hasRepo': bool(app['repo']), 'mode': app['mode'],
                'branch': app['repo']['branch'] if app['repo'] else None, 'hasHealthUrl': bool(app['health'] and app['health']['url']),
                'git': self.store.check(app_id) if app['repo'] else None, 'lastJob': jobs[0] if jobs else None, 'activeJob': active_job,
                'lastDeploy': self.store.last_deploy(app_id)}

    def job_id(self, request):
        job_id = request.get('id')
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
            raise RequestError('Invalid job id.')
        return job_id

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
        if op == 'run':
            action = request.get('action')
            if not isinstance(action, str):
                raise RequestError('Unknown action.')
            return {'job': self.submit(self.app_id(request), action, request.get('actor'))}
        if op == 'jobs':
            limit = request.get('limit', 20)
            return {'jobs': self.store.jobs(self.app_id(request), limit if isinstance(limit, int) and 0 < limit <= 100 else 20)}
        if op == 'job':
            job = self.store.job(self.job_id(request))
            if not job:
                raise RequestError('Unknown job.', 'not_found')
            return {'job': job}
        if op == 'log':
            offset, epoch = request.get('offset', 0), request.get('epoch')
            return self.read_log(self.job_id(request), offset if isinstance(offset, int) and offset >= 0 else 0,
                                 epoch=epoch if isinstance(epoch, int) and not isinstance(epoch, bool) else None)
        if op == 'cancel':
            return {'job': self.cancel(self.job_id(request))}
        raise RequestError('Unknown operation.')

    def close(self):
        self.stop.set()
        with self.active_lock:
            handles = list(self.active.values())
        for handle in handles:
            handle['cancelled'].set()
            if handle['proc']:
                self.kill(handle, handle['proc'], 'cancel')
        deadline = time.time() + 10
        while self.active and time.time() < deadline:
            time.sleep(0.05)
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


def bind(path, handler, group=None):
    """A Unix socket created 0660 from the start, optionally handed to a group shared with the web user."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    old = os.umask(0o117)
    try:
        server = SocketServer(str(path), handler)
    finally:
        os.umask(old)
    os.chmod(path, 0o660)
    if group:
        try:
            os.chown(path, -1, grp.getgrnam(group).gr_gid)
        except (KeyError, OSError) as exc:
            server.server_close()
            raise SystemExit(f'Cannot give {path} to group {group}: {exc}')
    return server


def serve(runner, path, group=None):
    server = bind(path, SocketHandler, group)
    server.runner = runner
    return server


class DockerProxyHandler(socketserver.BaseRequestHandler):
    """Read-only Docker socket for SIGNAL's web server. Only GET requests to the allow-listed reads pass;
    the request is rebuilt from the validated target, so no client header or body ever reaches Docker."""
    def handle(self):
        self.request.settimeout(10)
        data = b''
        try:
            while b'\r\n\r\n' not in data:
                chunk = self.request.recv(4096)
                if not chunk:
                    return
                data += chunk
                if len(data) > 16384:
                    return self.deny()
        except OSError:
            return
        head, _, rest = data.partition(b'\r\n\r\n')
        lines = head.decode('latin-1').split('\r\n')
        parts = lines[0].split(' ')
        headers = {line.partition(':')[0].strip().lower() for line in lines[1:]}
        if (len(parts) != 3 or parts[0] != 'GET' or not parts[2].startswith('HTTP/1.') or rest
                or headers & {'content-length', 'transfer-encoding', 'upgrade'} or not DOCKER_READ.fullmatch(parts[1])):
            return self.deny()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as upstream:
                upstream.settimeout(30)
                upstream.connect(self.server.docker_socket)
                upstream.sendall(f'GET {parts[1]} HTTP/1.1\r\nHost: docker\r\nConnection: close\r\n\r\n'.encode())
                while True:
                    chunk = upstream.recv(65536)
                    if not chunk:
                        break
                    self.request.sendall(chunk)
        except OSError:
            return

    def deny(self):
        body = b'{"message":"Refused by the SIGNAL read-only Docker proxy."}'
        try:
            self.request.sendall(b'HTTP/1.1 403 Forbidden\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n' % len(body) + body)
        except OSError:
            pass


def serve_docker_proxy(path, docker_socket=DOCKER_SOCKET, group=None):
    server = bind(path, DockerProxyHandler, group)
    server.docker_socket = docker_socket
    return server


def main():
    try:
        apps = load_registry(REGISTRY)
    except RegistryError as exc:
        raise SystemExit('Refusing to start: ' + str(exc))
    runner = Runner(apps, DATA_DIR)
    runner.start_checker()
    server = serve(runner, SOCKET_PATH, SOCKET_GROUP)
    print(f'SIGNAL runner managing {len(apps)} apps on {SOCKET_PATH}', flush=True)
    proxy = None
    if DOCKER_PROXY:
        proxy = serve_docker_proxy(DOCKER_PROXY, DOCKER_SOCKET, SOCKET_GROUP)
        threading.Thread(target=proxy.serve_forever, daemon=True, name='docker-proxy').start()
        print(f'Read-only Docker proxy on {DOCKER_PROXY}', flush=True)
    def shutdown(signum, frame):
        threading.Thread(target=server.shutdown, daemon=True).start()
        if proxy:
            threading.Thread(target=proxy.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        runner.close()


if __name__ == '__main__':
    main()
