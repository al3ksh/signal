#!/usr/bin/env python3
"""Semi-automatic disk cleaner for the Raspberry Pi. Scan (the default) never changes anything.

    sudo python3 pi-clean.py                 grouped report: sizes, safety level, proposed actions
    sudo python3 pi-clean.py --json          the same, machine-readable
    sudo python3 pi-clean.py apply           interactive: asks y/N per group, or per item
    sudo python3 pi-clean.py apply --safe    applies only items marked safe, printing each first
    pi-clean serve --home /home/alexpi       root daemon behind SIGNAL's cleanup card (pi-clean.service)

It automates the manual routine (df, du, docker images/ps, journal, atop, apt cache, deploy leftovers) but decides
per item and never wholesale: no `docker system prune`, no `image prune -a`, no volume pruning, and nothing under a
user's data directories. "Reclaimable" is computed from real container-to-image references, because
`docker system df` is wrong under the containerd image store. Every item is re-checked right before it is applied.
"""
import argparse
import datetime
import grp
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import socketserver
import subprocess
import sys
import time

DAY = 86400
JOURNAL_LIMIT = 100 * 1024 ** 2
STOPPED_DAYS, ATOP_DAYS, KEEP_RELEASES = 14, 7, 5
SAFE, REVIEW, REPORT = 'safe', 'review', 'report-only'
LEGACY = re.compile(r'(?:dist-before|backend-before|release)-[A-Za-z0-9._-]+')
STAMP = re.compile(r'\d{8}-\d{6}')
UNITS = {'': 1, 'b': 1, 'k': 1e3, 'kb': 1e3, 'kib': 1024, 'm': 1e6, 'mb': 1e6, 'mib': 1024 ** 2, 'g': 1e9, 'gb': 1e9, 'gib': 1024 ** 3, 't': 1e12, 'tb': 1e12, 'tib': 1024 ** 4}


def run_command(argv, timeout=120):
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
        return done.returncode, done.stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, str(exc)


def parse_size(text):
    """'4.5GB', '121.0M', '512kB', '1.2GiB' -> bytes (journalctl uses 1024-based single letters)."""
    match = re.fullmatch(r'\s*([\d.]+)\s*([A-Za-z]*)\s*', text or '')
    if not match:
        return None
    unit = match[2].lower()
    if unit in ('k', 'm', 'g', 't'):
        unit += 'ib'
    return int(float(match[1]) * UNITS[unit]) if unit in UNITS else None


def human(n):
    if n is None:
        return '?'
    for unit, size in (('GB', 1e9), ('MB', 1e6), ('kB', 1e3)):
        if n >= size:
            return f'{n / size:.1f} {unit}'
    return f'{n} B'


def du(path):
    total = 0
    path = Path(path)
    if path.is_file() or path.is_symlink():
        return path.lstat().st_size
    for folder, _, files in os.walk(path, onerror=lambda e: None):
        for name in files:
            try:
                total += os.lstat(os.path.join(folder, name)).st_size
            except OSError:
                pass
    return total


def normal_ref(ref):
    ref = ref.split('@')[0]
    for prefix in ('docker.io/library/', 'docker.io/'):
        if ref.startswith(prefix):
            ref = ref[len(prefix):]
    return ref if ':' in ref.rsplit('/', 1)[-1] else ref + ':latest'


def finding(id, category, title, size, safety, reason, action=None, age=None, **extra):
    return {'id': id, 'category': category, 'title': title, 'bytes': size, 'age': age, 'safety': safety, 'reason': reason,
            'action': action or [], **extra}


class Scanner:
    def __init__(self, root='/', home=None, signal_dir=None, registry='/etc/signal/apps.json', run=run_command, now=None):
        self.root, self.run, self.now = Path(root), run, now or time.time()
        self.home = Path(home) if home else default_home()
        self.signal_dir = Path(signal_dir) if signal_dir else self.home / 'apps' / 'signal'
        self.registry = registry

    def path(self, absolute):
        return self.root / str(absolute).lstrip('/')

    def json_lines(self, argv):
        code, out = self.run(argv)
        if code:
            return None
        out = out.strip()
        try:
            return json.loads(out) if out.startswith('[') else [json.loads(line) for line in out.splitlines() if line.strip()]
        except ValueError:
            return None

    # -- docker ---------------------------------------------------------------
    def docker_state(self):
        code, ids = self.run(['docker', 'image', 'ls', '-q', '--no-trunc'])
        if code:
            return None
        images = self.json_lines(['docker', 'image', 'inspect', *sorted(set(ids.split()))]) if ids.split() else []
        code, cids = self.run(['docker', 'ps', '-aq', '--no-trunc'])
        containers = self.json_lines(['docker', 'container', 'inspect', *cids.split()]) if code == 0 and cids.split() else []
        if images is None or containers is None:
            return None
        return images, containers, self.compose_refs()

    def compose_refs(self):
        """Image references of the registered apps (what their compose files would run), per app id."""
        try:
            apps = json.loads(self.path(self.registry).read_text()).get('apps', [])
        except (OSError, ValueError, AttributeError):
            return None
        refs = {}
        for app in apps:
            if not isinstance(app, dict) or not isinstance(app.get('id'), str) or not isinstance(app.get('dir'), str):
                continue
            argv = ['docker', 'compose', '--project-directory', app['dir'], '-p', app['id']]
            for name in app.get('composeFiles') or []:
                argv += ['-f', os.path.join(app['dir'], name)]
            code, out = self.run(argv + ['config', '--images'])
            refs[app['id']] = {normal_ref(r) for r in out.split()} if code == 0 else set()
        return refs

    def scan_docker(self):
        state = self.docker_state()
        if state is None:
            return [finding('docker-unavailable', 'docker-unused-image', 'Docker could not be read', None, REPORT, 'Run as a user with Docker access (sudo).')]
        images, containers, refs = state
        result = []
        code, out = self.run(['docker', 'system', 'df', '--format', '{{json .}}'])
        for row in (json.loads(line) for line in out.splitlines() if line.strip().startswith('{')) if code == 0 else []:
            if row.get('Type') == 'Build Cache' and (parse_size(str(row.get('Size', '0B')).split()[0]) or 0) > 0:
                result.append(finding('docker-build-cache', 'docker-build-cache', 'Docker build cache', parse_size(str(row['Size']).split()[0]), SAFE,
                                      'Intermediate build layers; rebuilt on demand.', [{'argv': ['docker', 'builder', 'prune', '-f']}]))
        used = {}
        for container in containers:
            used.setdefault(container.get('Image'), []).append(container)
        known = set().union(*refs.values()) if refs else set()
        repos = {ref.rsplit(':', 1)[0] for ref in known}
        app_ids = set(refs or {})
        dangling = [i for i in images if not [t for t in i.get('RepoTags') or [] if t != '<none>:<none>'] and i['Id'] not in used]
        if dangling:
            result.append(finding('docker-dangling', 'docker-dangling', f'{len(dangling)} dangling <none> image(s)', sum(i.get('Size', 0) for i in dangling), SAFE,
                                  'Untagged leftovers of rebuilds, used by no container.', [{'argv': ['docker', 'image', 'prune', '-f']}]))
        for image in images:
            tags = [t for t in image.get('RepoTags') or [] if t != '<none>:<none>']
            if not tags or image['Id'] in used:
                continue
            if any(normal_ref(t) in known for t in tags):
                continue  # what a registered app's compose file runs
            prev = [t for t in tags if t.endswith(':signal-prev')]
            if prev and (refs is None or any(self.belongs(t, repos, app_ids) for t in prev)):
                continue  # rollback image of a registered app (or the registry is unreadable: keep to be safe)
            created = parse_time(image.get('Created'))
            result.append(finding('image:' + image['Id'].split(':')[-1][:12], 'docker-unused-image', ', '.join(tags), image.get('Size'), REVIEW,
                                  'No container (running or stopped) and no registered app uses it.', [{'argv': ['docker', 'image', 'rm', *tags]}],
                                  age=self.now - created if created else None))
        stopped = {}
        for container in containers:
            state, finished = container.get('State', {}), parse_time(container.get('State', {}).get('FinishedAt'))
            if state.get('Status') in ('exited', 'dead', 'created') and finished and self.now - finished > STOPPED_DAYS * DAY:
                stopped[container['Id']] = (container, finished)
        projects = {}
        for container in containers:
            project = (container.get('Config', {}).get('Labels') or {}).get('com.docker.compose.project')
            if project:
                projects.setdefault(project, []).append(container['Id'])
        sizes = {i['Id']: i.get('Size', 0) for i in images}
        def pinned(ids):
            """Image bytes freed once these containers are gone (images no other container uses)."""
            images_ = {stopped[i][0].get('Image') for i in ids}
            return sum(sizes.get(img, 0) for img in images_ if all(c['Id'] in ids for c in used.get(img, [])))
        done = set()
        for project, members in sorted(projects.items()):
            if members and all(m in stopped for m in members):
                names = [stopped[m][0].get('Name', '').lstrip('/') for m in members]
                result.append(finding('project:' + project, 'docker-stopped', f'Compose project {project}: {", ".join(names)}', pinned(set(members)), REVIEW,
                                      f'All its containers exited over {STOPPED_DAYS} days ago. Pins: {self.image_names(stopped, members, images)}. '
                                      'Volumes are kept; the images become separate findings on the next scan.',
                                      [{'argv': ['docker', 'compose', '-p', project, 'down']}], age=self.now - max(stopped[m][1] for m in members), project=project))
                done.update(members)
        for cid, (container, finished) in sorted(stopped.items()):
            if cid in done:
                continue
            name = container.get('Name', '').lstrip('/')
            result.append(finding('container:' + name, 'docker-stopped', f'Stopped container {name}', pinned({cid}), REVIEW,
                                  f'Exited {int((self.now - finished) // DAY)} days ago. Pins: {self.image_names(stopped, [cid], images)}.',
                                  [{'argv': ['docker', 'rm', cid]}], age=self.now - finished))
        code, out = self.run(['docker', 'volume', 'ls', '-q', '--filter', 'dangling=true'])
        for name in out.split() if code == 0 else []:
            result.append(finding('volume:' + name, 'docker-volumes', f'Dangling volume {name}', None, REPORT,
                                  'Not attached to any container. Volumes hold data, so this tool never removes them.'))
        return result

    @staticmethod
    def belongs(tag, repos, app_ids):
        repo = normal_ref(tag).rsplit(':', 1)[0]
        name = repo.split('/')[-1]
        return repo in repos or any(name == app or name.startswith(app + '-') for app in app_ids)

    @staticmethod
    def image_names(stopped, ids, images):
        tags = {i['Id']: (i.get('RepoTags') or [i['Id'][:19]])[0] for i in images}
        return ', '.join(sorted({f"{tags.get(stopped[c][0].get('Image'), '?')} ({human(next((i.get('Size') for i in images if i['Id'] == stopped[c][0].get('Image')), None))})" for c in ids}))

    # -- system -----------------------------------------------------------------
    def scan_journal(self):
        result = []
        code, out = self.run(['journalctl', '--disk-usage'])
        match = re.search(r'take up ([\d.]+\s*[A-Za-z]+)', out) if code == 0 else None
        usage = parse_size(match[1]) if match else None
        if usage and usage > JOURNAL_LIMIT:
            result.append(finding('journal', 'journal', f'systemd journal ({human(usage)})', usage - JOURNAL_LIMIT, SAFE,
                                  'Keeps the newest 100 MB of logs.', [{'argv': ['journalctl', '--vacuum-size=100M']}]))
        configs = [self.path('/etc/systemd/journald.conf')] + sorted(self.path('/etc/systemd/journald.conf.d').glob('*.conf'))
        limited = any(re.search(r'^\s*SystemMaxUse\s*=', p.read_text(errors='replace'), re.M) for p in configs if p.is_file())
        if not limited and (usage is not None or self.path('/var/log/journal').exists()):
            result.append(finding('journal-limit', 'journal-limit', 'journald has no size limit', None, REVIEW,
                                  'Persistent config: caps the journal at 100 MB so it cannot grow back.',
                                  [{'write': '/etc/systemd/journald.conf.d/size.conf', 'content': '[Journal]\nSystemMaxUse=100M\n'},
                                   {'argv': ['systemctl', 'restart', 'systemd-journald']}]))
        return result

    def scan_atop(self):
        result = []
        folder = self.path('/var/log/atop')
        old = [p for p in sorted(folder.glob('atop_*')) if p.is_file() and self.now - p.stat().st_mtime > ATOP_DAYS * DAY] if folder.is_dir() else []
        if old:
            result.append(finding('atop', 'atop', f'{len(old)} atop log(s) older than {ATOP_DAYS} days', sum(p.stat().st_size for p in old), SAFE,
                                  'Old daily atop recordings.', [{'delete': ['/' + str(p.relative_to(self.root)) for p in old]}],
                                  age=self.now - min(p.stat().st_mtime for p in old)))
        config = self.path('/etc/default/atop')
        if config.is_file():
            text = config.read_text(errors='replace')
            match = re.search(r'^\s*LOGGENERATIONS\s*=\s*"?(\d+)"?', text, re.M)
            if match and int(match[1]) > ATOP_DAYS:
                result.append(finding('atop-limit', 'atop-limit', f'atop keeps {match[1]} days', None, REVIEW,
                                      f'Persistent config: keep {ATOP_DAYS} days of atop logs.',
                                      [{'write': '/etc/default/atop', 'content': re.sub(r'^(\s*LOGGENERATIONS\s*=\s*)"?\d+"?', rf'\g<1>{ATOP_DAYS}', text, flags=re.M)}]))
        return result

    def scan_apt(self):
        debs = list(self.path('/var/cache/apt/archives').glob('*.deb'))
        size = sum(p.stat().st_size for p in debs)
        return [finding('apt', 'apt', f'{len(debs)} cached .deb package(s)', size, SAFE, 'Downloaded packages; apt fetches them again if needed.',
                        [{'argv': ['apt-get', 'clean']}])] if debs else []

    def scan_signal(self):
        folder = self.path(self.signal_dir)
        if not folder.is_dir():
            return []
        candidates = [p for p in folder.iterdir() if p.is_dir() and LEGACY.fullmatch(p.name)]
        releases = folder / 'releases'
        if releases.is_dir():
            candidates += [p for p in releases.iterdir() if p.is_dir() and STAMP.fullmatch(p.name)]
        candidates.sort(key=lambda p: p.stat().st_mtime)
        old = candidates[:-KEEP_RELEASES]
        if not old:
            return []
        return [finding('signal-releases', 'signal-releases', f'{len(old)} old SIGNAL deploy backup(s)', sum(du(p) for p in old), SAFE,
                        f'Keeps the newest {KEEP_RELEASES} backups (releases/ and the old *-before-* / release-* dirs).',
                        [{'delete': [self.absolute(p) for p in old]}], age=self.now - old[0].stat().st_mtime)]

    def absolute(self, path):
        """Host-absolute form of a path under root (identity when root is /)."""
        return '/' + str(Path(path).relative_to(self.root))

    def scan_pycache(self):
        apps = self.path(self.home) / 'apps'
        found = []
        for folder, dirs, _ in os.walk(apps) if apps.is_dir() else []:
            dirs[:] = [d for d in dirs if d not in ('node_modules', '.git')]
            if os.path.basename(folder) == '__pycache__':
                found.append(Path(folder))
                dirs[:] = []
        return [finding('pycache', 'pycache', f'{len(found)} __pycache__ folder(s) under ~/apps', sum(du(p) for p in found), SAFE,
                        'Python bytecode caches; recreated automatically.', [{'delete': [self.absolute(p) for p in found]}])] if found else []

    def scan_home_backups(self):
        home = self.path(self.home)
        paths = sorted(list((home / 'backups').glob('*')) + [p for p in (home / 'apps').glob('*backup*')]) if home.is_dir() else []
        return [finding('backup:' + str(p.relative_to(home)), 'home-backups', f'~/{p.relative_to(home)}', du(p), REPORT, 'Your backup: decide by hand.',
                        age=self.now - p.stat().st_mtime) for p in paths if p.exists()]

    def scan_big_files(self, covered):
        code, out = self.run(['find', str(self.root), '-xdev', '-type', 'f', '-size', '+100M', '-printf', '%s\t%p\n'], timeout=300)
        rows = []
        for line in out.splitlines() if code in (0, 1) else []:
            size, _, path = line.partition('\t')
            shown = self.absolute(path) if path.startswith(str(self.root)) else path
            if size.isdigit() and not any(shown.startswith(prefix) for prefix in covered):
                rows.append((int(size), shown))
        return [finding('file:' + path, 'big-files', path, size, REPORT, 'Large file not covered by another finding.') for size, path in sorted(rows, reverse=True)[:15]]

    def scan(self, categories=None):
        scanners = [('docker', self.scan_docker), ('journal', self.scan_journal), ('atop', self.scan_atop), ('apt', self.scan_apt),
                    ('signal-releases', self.scan_signal), ('pycache', self.scan_pycache), ('home-backups', self.scan_home_backups)]
        result = []
        for name, scanner in scanners:
            if categories is None or name in categories:
                result += scanner()
        if categories is None or 'big-files' in categories:
            covered = ['/var/lib/containerd', '/var/lib/docker', '/var/log/journal', '/var/log/atop', '/var/cache/apt', '/proc', '/swapfile', '/var/swap',
                       str(self.home / 'backups')] + [str(p) for f in result for step in f['action'] for p in step.get('delete', [])]
            result += self.scan_big_files(covered)
        return result


CATEGORY_SCANNER = {'docker-build-cache': 'docker', 'docker-dangling': 'docker', 'docker-unused-image': 'docker', 'docker-stopped': 'docker',
                    'docker-volumes': 'docker', 'journal': 'journal', 'journal-limit': 'journal', 'atop': 'atop', 'atop-limit': 'atop',
                    'apt': 'apt', 'signal-releases': 'signal-releases', 'pycache': 'pycache', 'home-backups': 'home-backups', 'big-files': 'big-files'}


def parse_time(value):
    if not value or value.startswith('0001-'):
        return None
    try:
        text = re.sub(r'\.(\d{6})\d*', r'.\1', value.replace('Z', '+00:00'))
        return datetime.datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def default_home():
    user = os.environ.get('SUDO_USER')
    try:
        return Path(pwd.getpwnam(user).pw_dir) if user else Path.home()
    except KeyError:
        return Path.home()


class Applier:
    """Runs one finding's steps after re-checking it, with guarded deletes, and logs bytes freed."""
    def __init__(self, scanner, out=print, log='/var/log/pi-clean.log'):
        self.scanner, self.out, self.log = scanner, out, scanner.path(log)
        self.allowed = [str(scanner.signal_dir).rstrip('/') + '/', '/var/log/atop/']

    def deletable(self, path):
        path = os.path.normpath(path)
        return path.endswith('/__pycache__') or any(path.startswith(prefix) and path != prefix.rstrip('/') for prefix in self.allowed)

    def fresh(self, item):
        """Re-scan the finding's category; the item still has to qualify with the same action."""
        current = {f['id']: f for f in self.scanner.scan({CATEGORY_SCANNER[item['category']]})}
        again = current.get(item['id'])
        return again if again and again['safety'] == item['safety'] else None

    def apply(self, item):
        if item['safety'] == REPORT or not item['action']:
            return False
        current = self.fresh(item)
        if current is None:
            self.out(f"  skip {item['id']}: no longer applies")
            return False
        free = shutil.disk_usage(self.scanner.root).free
        for step in current['action']:
            if 'argv' in step:
                self.out('  $ ' + ' '.join(step['argv']))
                code, output = self.scanner.run(step['argv'])
                if code:
                    self.out(f'  failed ({code}): {output.strip()[-300:]}')
                    self.record(item, 0, 'failed')
                    return False
            elif 'delete' in step:
                for path in step['delete']:
                    if not self.deletable(path):
                        self.out(f'  refused to delete {path}: outside the allowed places')
                        continue
                    target = self.scanner.path(path)
                    self.out(f'  delete {path}')
                    shutil.rmtree(target, ignore_errors=True) if target.is_dir() and not target.is_symlink() else target.unlink(missing_ok=True)
            elif 'write' in step:
                target = self.scanner.path(step['write'])
                self.out(f"  write {step['write']}")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(step['content'])
        freed = max(0, shutil.disk_usage(self.scanner.root).free - free)
        self.record(item, freed, 'applied')
        self.out(f'  done, {human(freed)} freed')
        return True

    def record(self, item, freed, status):
        try:
            with open(self.log, 'a') as log:
                log.write(f"{datetime.datetime.now().isoformat(timespec='seconds')}\t{status}\t{item['id']}\t{freed}\t{json.dumps(item['action'])}\n")
        except OSError:
            pass


class Service:
    """The root side of SIGNAL's cleanup card. Only two requests exist: scan, and apply one finding by id. The id is
    looked up in this daemon's own scan, so a client never supplies a command or a path, and report-only items stay
    untouchable here too."""
    def __init__(self, make_scanner, max_age=900, clock=time.time):
        self.make_scanner, self.max_age, self.clock, self.cache = make_scanner, max_age, clock, None

    def scan(self, refresh=False):
        if refresh or self.cache is None or self.clock() - self.cache['scannedAt'] > self.max_age:
            scanner = self.make_scanner()
            free = shutil.disk_usage(scanner.root).free
            self.cache = {'scannedAt': self.clock(), 'free': free, 'findings': scanner.scan()}
        return self.cache

    def apply(self, id):
        item = next((f for f in self.scan()['findings'] if f['id'] == id), None)
        if item is None or item['safety'] == REPORT or not item['action']:
            return {'ok': False, 'code': 'not_found', 'error': 'No cleanable item with that id. Rescan and try again.'}
        lines = []
        scanner = self.make_scanner()
        before = shutil.disk_usage(scanner.root).free
        applied = Applier(scanner, lines.append).apply(item)
        free = shutil.disk_usage(scanner.root).free
        # The item is done (or no longer applies) either way; the rest of the cached scan stays valid until a refresh.
        self.cache['findings'] = [f for f in self.cache['findings'] if f['id'] != id]
        self.cache['free'] = free
        return {'ok': True, 'applied': applied, 'id': id, 'title': item['title'], 'freed': max(0, free - before), 'free': free, 'log': lines}

    def handle(self, request):
        if not isinstance(request, dict):
            return {'ok': False, 'code': 'invalid', 'error': 'Invalid request.'}
        if request.get('op') == 'scan':
            return {'ok': True, **self.scan(bool(request.get('refresh')))}
        if request.get('op') == 'apply' and isinstance(request.get('id'), str) and 0 < len(request['id']) <= 512:
            return self.apply(request['id'])
        return {'ok': False, 'code': 'invalid', 'error': 'Invalid request.'}


def make_server(service, path, group=None):
    """One request at a time over a unix socket: newline-delimited JSON in, one JSON line out."""
    class Handler(socketserver.StreamRequestHandler):
        timeout = 10

        def handle(self):
            try:
                line = self.rfile.readline(4096)
                request = json.loads(line) if line.endswith(b'\n') else None
            except (OSError, ValueError):
                request = None
            self.request.settimeout(None)  # applying can take minutes; the reply must still arrive
            try:
                response = service.handle(request)
            except Exception as exc:  # one broken scanner must not take the daemon down
                response = {'ok': False, 'code': 'failed', 'error': f'{type(exc).__name__}: {exc}'[:300]}
            self.wfile.write(json.dumps(response).encode() + b'\n')

    if os.path.exists(path):
        os.unlink(path)
    server = socketserver.UnixStreamServer(path, Handler)
    os.chmod(path, 0o660)
    if group:
        os.chown(path, 0, grp.getgrnam(group).gr_gid)
    return server


def serve(service, path, group=None):
    server = make_server(service, path, group)
    print(f'pi-clean: serving on {path}', flush=True)
    server.serve_forever()


def report(findings, out=print):
    total = {level: sum(f['bytes'] or 0 for f in findings if f['safety'] == level) for level in (SAFE, REVIEW, REPORT)}
    out(f"Safe to clean: {human(total[SAFE])} · needs review: {human(total[REVIEW])} · report only: {human(total[REPORT])}")
    for category in dict.fromkeys(f['category'] for f in findings):
        items = [f for f in findings if f['category'] == category]
        out(f"\n[{category}] {human(sum(f['bytes'] or 0 for f in items))}")
        for f in items:
            age = f" · {int(f['age'] // DAY)} d old" if f.get('age') else ''
            out(f"  {f['safety']:<11} {human(f['bytes']):>9}  {f['title']}{age}")
            out(f"              {f['reason']}")
            for step in f['action']:
                out('              -> ' + (' '.join(step['argv']) if 'argv' in step else f"delete {len(step['delete'])} path(s)" if 'delete' in step else f"write {step['write']}"))


def main(argv=None, scanner=None, out=print, ask=input):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('mode', nargs='?', choices=['scan', 'apply', 'serve'], default='scan')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--safe', action='store_true', help='with apply: only items marked safe, no questions')
    parser.add_argument('--home', help='home whose ~/apps and ~/backups are scanned (default: the sudo user)')
    parser.add_argument('--signal-dir', help='SIGNAL install dir (default ~/apps/signal)')
    parser.add_argument('--registry', default='/etc/signal/apps.json')
    parser.add_argument('--socket', default='/run/pi-clean/control.sock', help='with serve: where SIGNAL connects')
    parser.add_argument('--group', help='with serve: group allowed to connect (the user SIGNAL runs as)')
    args = parser.parse_args(argv)
    if args.mode == 'serve':
        serve(Service(lambda: scanner or Scanner(home=args.home, signal_dir=args.signal_dir, registry=args.registry)), args.socket, args.group)
        return 0
    scanner = scanner or Scanner(home=args.home, signal_dir=args.signal_dir, registry=args.registry)
    before = shutil.disk_usage(scanner.root).free
    findings = scanner.scan()
    if args.mode == 'scan':
        if args.json:
            out(json.dumps({'scannedAt': scanner.now, 'free': before, 'findings': findings}, indent=1))
        else:
            out(f'Free space: {human(before)}')
            report(findings, out)
            out('\nNothing was changed. Run "apply" to choose items, or "apply --safe" for the safe ones.')
        return 0
    applier = Applier(scanner, out)
    actionable = [f for f in findings if f['safety'] != REPORT and f['action']]
    if args.safe:
        actionable = [f for f in actionable if f['safety'] == SAFE]
    for category in dict.fromkeys(f['category'] for f in actionable):
        items = [f for f in actionable if f['category'] == category]
        out(f"\n[{category}] {len(items)} item(s), {human(sum(f['bytes'] or 0 for f in items))}")
        for f in items:
            out(f"  {f['safety']}: {f['title']} ({human(f['bytes'])}) - {f['reason']}")
        choice = 'y' if args.safe else ask('Apply? [y]es all / [e]ach / [N]o: ').strip().lower()
        for f in items:
            if choice in ('y', 'yes') or (choice in ('e', 'each') and ask(f"  {f['title']}? [y/N]: ").strip().lower() in ('y', 'yes')):
                applier.apply(f)
    after = shutil.disk_usage(scanner.root).free
    out(f'\nFree space: {human(before)} -> {human(after)} ({human(max(0, after - before))} freed). Log: /var/log/pi-clean.log')
    return 0


if __name__ == '__main__':
    sys.exit(main())
