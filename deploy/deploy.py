#!/usr/bin/env python3
"""Deploy SIGNAL to a native systemd install: build locally, ship only what changed, restart only what needs
it, verify, and roll back by itself if verification fails.

    python deploy/deploy.py alexpi@100.105.170.124            build, ship, restart what changed, verify
    python deploy/deploy.py HOST --dry-run                     show what would change, touch nothing on the host
    python deploy/deploy.py HOST --skip-build                  dist/ is already built
    python deploy/deploy.py HOST --list                        releases on the host
    python deploy/deploy.py HOST --rollback [STAMP]            back to the newest (or named) release
    python deploy/deploy.py HOST --prune-legacy                also remove old dist-before-*/backend-before-*/release-* dirs

Standard library only. The only transport is ssh: a tar stream built here (no local tar, rsync or scp), so it
runs from Windows Git Bash as well as Linux and macOS. Each deploy first snapshots the host's current dist/ and
Python files into releases/<stamp>/; the newest five are kept.
"""
import argparse
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = '/home/alexpi/apps/signal'
KEEP = 5
STAMP = re.compile(r'\d{8}-\d{6}')
LEGACY = re.compile(r'(?:dist-before|backend-before|release)-[A-Za-z0-9._-]+')
UNITS = {'runner.py': 'signal-runner.service'}       # every other server-side module belongs to signal.service
NO_RESTART = {'setup_access.py', 'setup_2fa.py'}       # command-line tools, not loaded by a service
ASSET = re.compile(r'assets/index-[A-Za-z0-9_-]+\.js')


class DeployError(Exception):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


class Remote:
    """Runs POSIX sh snippets on the host. Tests swap SshRemote for LocalRemote and run the same snippets."""
    env = None

    def argv(self, script):
        raise NotImplementedError

    def run(self, script, data=None, check=True):
        done = subprocess.run(self.argv(script), input=data, capture_output=True, env=self.env)
        out, err = done.stdout.decode('utf-8', 'replace'), done.stderr.decode('utf-8', 'replace')
        if check and done.returncode:
            raise DeployError((err or out).strip()[-600:] or f'remote command exited with {done.returncode}')
        return done.returncode, out, err

    def hold(self, script):
        return subprocess.Popen(self.argv(script), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env)


class SshRemote(Remote):
    def __init__(self, host):
        self.host = host

    def argv(self, script):
        return ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', self.host, 'sh -c ' + shlex.quote(script)]


class LocalRemote(Remote):
    def __init__(self, env=None):
        self.env = env

    def argv(self, script):
        return ['sh', '-c', script]


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as file:
        for chunk in iter(lambda: file.read(65536), b''):
            digest.update(chunk)
    return digest.hexdigest()


def local_manifest(root):
    """Every non-test *.py at the repository root plus dist/, derived from the files themselves."""
    files = sorted(p.name for p in root.glob('*.py') if p.is_file() and not p.name.startswith('test_'))
    dist = sorted(p.relative_to(root).as_posix() for p in (root / 'dist').rglob('*') if p.is_file())
    if not dist:
        raise DeployError('dist/ is empty. Build first or drop --skip-build.')
    return {name: sha256(root / name) for name in files + dist}


def parse_hashes(text, prefix=''):
    result = {}
    for line in text.splitlines():
        digest, _, name = line.partition('  ')
        name = name.strip()
        if len(digest) == 64 and name:
            name = name[2:] if name.startswith('./') else name
            result[name[len(prefix):] if prefix and name.startswith(prefix) else name] = digest
    return result


def hashes_script(base):
    return (f"cd {shlex.quote(base)} 2>/dev/null || exit 0\n"
            "for f in *.py; do [ -f \"$f\" ] && sha256sum \"$f\"; done\n"
            "if [ -d dist ]; then find dist -type f -exec sha256sum {} +; fi\n"
            "exit 0")


def classify(local, remote):
    python = sorted(f for f in local if '/' not in f and local[f] != remote.get(f))
    dist = {f: h for f, h in local.items() if f.startswith('dist/')} != {f: h for f, h in remote.items() if f.startswith('dist/')}
    units = sorted({UNITS.get(f, 'signal.service') for f in python if f not in NO_RESTART})
    return python, dist, units


def git_sha(root):
    try:
        return subprocess.run(['git', 'rev-parse', '--short=12', 'HEAD'], cwd=root, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def expected_asset(index_html):
    match = ASSET.search(index_html or '')
    return match.group(0) if match else None


class Deployer:
    def __init__(self, args, remote, root, out=print, confirm=input):
        self.args, self.remote, self.root, self.out, self.confirm = args, remote, Path(root), out, confirm
        self.dir = args.dir
        self.q = shlex.quote(args.dir)

    # -- local ---------------------------------------------------------------
    def preflight(self):
        if not self.args.allow_dirty and (self.root / '.git').exists():
            dirty = subprocess.run(['git', 'status', '--porcelain'], cwd=self.root, capture_output=True, text=True).stdout.strip()
            if dirty:
                raise DeployError('The working tree has uncommitted changes. Commit them or pass --allow-dirty.\n' + dirty, 2)
        if not self.args.skip_build:
            npm = shutil.which('npm')
            if not npm:
                raise DeployError('npm was not found. Install Node.js or pass --skip-build with a built dist/.', 2)
            self.out('- npm run build')
            if subprocess.run([npm, 'run', 'build'], cwd=self.root).returncode:
                raise DeployError('npm run build failed.')
        if not self.args.skip_tests:
            self.out('- python -m unittest -q')
            if subprocess.run([sys.executable, '-m', 'unittest', '-q'], cwd=self.root).returncode:
                raise DeployError('Tests failed. Fix them or pass --skip-tests.')

    # -- host ----------------------------------------------------------------
    def remote_hashes(self, base=None):
        return parse_hashes(self.remote.run(hashes_script(base or self.dir))[1])

    def lock(self):
        holder = self.remote.hold(f"mkdir -p {self.q} && exec flock -n {self.q}/.deploy.lock sh -c 'echo locked; exec cat >/dev/null'")
        if holder.stdout.readline().strip() != b'locked':
            holder.kill()
            raise DeployError('Another deploy holds the lock on the host (.deploy.lock). Try again when it is done.')
        return holder

    def check_sudo(self, units):
        if not units:
            return
        script = ''.join(f"sudo -n -l systemctl restart {u} >/dev/null 2>&1 || echo {u}\n" for u in units)
        refused = self.remote.run(script, check=False)[1].split()
        if refused:
            commands = '\n'.join(f'  sudo systemctl restart {u}' for u in refused)
            raise DeployError('sudo would ask for a password, so nothing was changed. Either allow the restarts without a password\n'
                              f"(sudo visudo -f /etc/sudoers.d/signal-deploy, line: alexpi ALL=(root) NOPASSWD: "
                              f"{', '.join('/usr/bin/systemctl restart ' + u for u in refused)})\n"
                              f'or deploy, then run on the host:\n{commands}', 2)

    def snapshot(self, stamp, info):
        """releases/<stamp>/ holds the host's current dist/ and Python files plus manifest.json."""
        release = f'{self.dir}/releases/{stamp}'
        self.remote.run(f"set -e\ncd {self.q}\nmkdir -p releases/{stamp}\n"
                        f"if [ -d dist ]; then cp -a dist releases/{stamp}/dist; fi\n"
                        f"for f in *.py; do [ -f \"$f\" ] && cp -p \"$f\" releases/{stamp}/; done\n"
                        f"cat > releases/{stamp}/manifest.json", data=json.dumps(info, indent=1).encode())
        return release

    def upload(self, stamp, files):
        buffer = io.BytesIO()
        def normal(info):
            info.uid = info.gid = 0
            info.uname = info.gname = ''
            info.mode = 0o644
            return info
        with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
            for name in files:
                archive.add(self.root / name, arcname=name, filter=normal)
        incoming = f'releases/{stamp}.incoming'
        self.remote.run(f"set -e\ncd {self.q}\nrm -rf {incoming}\nmkdir -p {incoming}\ntar -xzf - -C {incoming}", data=buffer.getvalue())
        sums = ''.join(f'{self.local[name]}  {name}\n' for name in files).encode()
        code, out, err = self.remote.run(f"cd {self.q}/{incoming} && sha256sum -c --quiet -", data=sums, check=False)
        if code:
            self.remote.run(f"rm -rf {self.q}/{incoming}", check=False)
            raise DeployError('Uploaded files do not match their local hashes; nothing was swapped in.\n' + (out + err).strip()[-400:])
        return incoming

    def swap(self, incoming, python, dist):
        lines = ['set -e', f'cd {self.q}', f'I={incoming}']
        if dist:
            lines += ['rm -rf dist.old', 'if [ -d dist ]; then mv dist dist.old; fi', 'mv "$I/dist" dist', 'rm -rf dist.old']
        lines += [f'install -m 644 "$I/{name}" {shlex.quote(name)}' for name in python]
        lines += ['rm -rf "$I"']
        self.remote.run('\n'.join(lines))

    def restart(self, units):
        if units:
            self.out('- restarting ' + ', '.join(units))
            self.remote.run('sudo -n systemctl restart ' + ' '.join(units))

    def verify(self, asset, units):
        url = f'http://127.0.0.1:{self.args.port}'
        script = (f"ok=\nfor i in $(seq 1 {self.args.verify_seconds}); do\n"
                  f"  if curl -fsS -o /dev/null {url}/healthz; then ok=1; break; fi\n  sleep 1\ndone\n"
                  f"[ -n \"$ok\" ] || {{ echo 'healthz did not answer 200 within {self.args.verify_seconds} s'; exit 1; }}\n")
        if asset:
            script += f"curl -fsS {url}/ | grep -q {shlex.quote(asset)} || {{ echo 'the served index.html does not reference {asset}'; exit 1; }}\n"
        for unit in units:
            script += f"systemctl is-active --quiet {unit} || {{ echo '{unit} is not active'; exit 1; }}\n"
        code, out, err = self.remote.run(script, check=False)
        return None if code == 0 else (out + err).strip() or 'verification failed'

    def restore(self, stamp):
        self.remote.run(f"set -e\ncd {self.q}\nB=releases/{stamp}\n"
                        "if [ -d \"$B/dist\" ]; then rm -rf dist.failed; if [ -d dist ]; then mv dist dist.failed; fi; cp -a \"$B/dist\" dist; rm -rf dist.failed; fi\n"
                        "for f in \"$B\"/*.py; do [ -f \"$f\" ] && install -m 644 \"$f\" \"$(basename \"$f\")\"; done\nexit 0")

    def releases(self):
        _, out, _ = self.remote.run(f"cd {self.q}/releases 2>/dev/null || exit 0\nfor d in *; do [ -d \"$d\" ] || continue; "
                                    "printf '%s\\t' \"$d\"; tr -d '\\n' < \"$d/manifest.json\" 2>/dev/null; echo; done")
        result = []
        for line in out.splitlines():
            name, _, text = line.partition('\t')
            if STAMP.fullmatch(name):
                try:
                    info = json.loads(text) if text.strip() else {}
                except ValueError:
                    info = {}
                result.append((name, info))
        return sorted(result)

    def prune(self, keep_also=()):
        _, out, _ = self.remote.run(f"cd {self.q}/releases 2>/dev/null || exit 0\nls -1")
        names = sorted(n for n in out.split() if STAMP.fullmatch(n))
        stale = [n for n in out.split() if n.endswith('.incoming') and STAMP.fullmatch(n[:-9]) and n[:-9] not in keep_also]
        old = [n for n in names[:-KEEP]] + stale
        if old:
            self.remote.run(f"cd {self.q}/releases && rm -rf " + ' '.join(shlex.quote(n) for n in old))
        return old

    def prune_legacy(self):
        _, out, _ = self.remote.run(f"cd {self.q} 2>/dev/null || exit 0\nfor d in dist-before-* backend-before-* release-*; do [ -d \"$d\" ] && du -sk \"$d\"; done; exit 0")
        found = [(int(kb), name) for kb, _, name in (line.partition('\t') for line in out.splitlines()) if kb.isdigit() and LEGACY.fullmatch(name)]
        if not found:
            self.out('No legacy backup dirs.')
            return
        for kb, name in found:
            self.out(f'  {kb / 1024:8.1f} MiB  {name}')
        self.out(f'  {sum(kb for kb, _ in found) / 1024:8.1f} MiB  in {len(found)} dirs')
        if not self.args.yes and self.confirm('Delete these legacy dirs? [y/N] ').strip().lower() not in ('y', 'yes'):
            self.out('Kept them.')
            return
        self.remote.run(f"cd {self.q} && rm -rf " + ' '.join(shlex.quote(name) for _, name in found))
        self.out(f'Removed {len(found)} legacy dirs.')

    # -- commands ------------------------------------------------------------
    def stamp(self):
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M%S')
        existing = {name for name, _ in self.releases()}
        while stamp in existing:  # two deploys within one second
            stamp = (datetime.datetime.strptime(stamp, '%Y%m%d-%H%M%S') + datetime.timedelta(seconds=1)).strftime('%Y%m%d-%H%M%S')
        return stamp

    def deploy(self):
        self.preflight()
        self.local = local_manifest(self.root)
        holder = None if self.args.dry_run else self.lock()
        try:
            remote = self.remote_hashes()
            python, dist, units = classify(self.local, remote)
            self.out(f"- host {self.dir}: {len(python)} Python file(s) changed{': ' + ', '.join(python) if python else ''}; "
                     f"frontend {'changed' if dist else 'unchanged'}; restart: {', '.join(units) or 'none'}")
            if not python and not dist:
                self.out('Nothing to deploy.')
                if self.args.prune_legacy and not self.args.dry_run:
                    self.prune_legacy()
                return 0
            if self.args.dry_run:
                self.out('Dry run: nothing was changed on the host.')
                return 0
            self.check_sudo(units)
            stamp = self.stamp()
            sha = git_sha(self.root)
            self.snapshot(stamp, {'stamp': stamp, 'kind': 'deploy', 'deploying': sha, 'python': python, 'dist': dist, 'units': units,
                                  'previous': {k: v for k, v in remote.items() if '/' not in k}})
            self.out(f'- snapshot releases/{stamp}')
            files = python + ([f for f in self.local if f.startswith('dist/')] if dist else [])
            incoming = self.upload(stamp, files)
            self.out(f'- uploaded {len(files)} file(s), hashes verified')
            self.swap(incoming, python, dist)
            self.restart(units)
            asset = expected_asset((self.root / 'dist' / 'index.html').read_text(errors='replace'))
            problem = self.verify(asset, units)
            if problem:
                self.out(f'FAIL verification failed: {problem}\n- rolling back to releases/{stamp}')
                self.restore(stamp)
                self.restart(units)
                again = self.verify(self.release_asset(stamp), units)
                raise DeployError(f'Deploy failed verification ({problem}) and was rolled back to releases/{stamp}.'
                                  + (f' The rollback did not verify either: {again}. Check journalctl -u signal on the host.' if again else ' The previous version is running.'))
            pruned = self.prune()
            self.out(f"OK deployed {sha or 'working tree'}: {len(python)} Python file(s), frontend {'updated' if dist else 'unchanged'}, "
                     f"restarted {', '.join(units) or 'nothing'}" + (f'; pruned {len(pruned)} old release(s)' if pruned else ''))
            self.out(f'  roll back with: python deploy/deploy.py {self.args.host} --rollback {stamp}')
            if self.args.prune_legacy:
                self.prune_legacy()
            return 0
        finally:
            if holder:
                holder.stdin.close()
                holder.wait(10)

    def release_asset(self, stamp):
        code, out, _ = self.remote.run(f"cat {self.q}/releases/{stamp}/dist/index.html", check=False)
        return expected_asset(out) if code == 0 else None

    def rollback(self):
        holder = self.lock()
        try:
            names = [name for name, _ in self.releases()]
            target = self.args.rollback if self.args.rollback != 'latest' else (names[-1] if names else None)
            if not target or target not in names:
                raise DeployError(f"No release {target or ''} on the host. See --list.".replace('  ', ' '), 2)
            current = self.remote_hashes()
            saved = self.remote_hashes(f'{self.dir}/releases/{target}')
            python = sorted(f for f in saved if '/' not in f and saved[f] != current.get(f))
            units = sorted({UNITS.get(f, 'signal.service') for f in python if f not in NO_RESTART})
            self.check_sudo(units)
            stamp = self.stamp()
            self.snapshot(stamp, {'stamp': stamp, 'kind': 'rollback', 'restoring': target, 'units': units,
                                  'previous': {k: v for k, v in current.items() if '/' not in k}})
            self.out(f'- snapshot of the current state: releases/{stamp} (roll back to it to undo this)')
            self.restore(target)
            self.restart(units)
            problem = self.verify(self.release_asset(target), units)
            if problem:
                self.restore(stamp)
                self.restart(units)
                raise DeployError(f'The rolled-back version did not verify ({problem}); restored releases/{stamp} again.')
            self.prune(keep_also=())
            self.out(f"OK rolled back to releases/{target}; restarted {', '.join(units) or 'nothing'}")
            self.out(f'  undo with: python deploy/deploy.py {self.args.host} --rollback {stamp}')
            return 0
        finally:
            holder.stdin.close()
            holder.wait(10)

    def list(self):
        rows = self.releases()
        if not rows:
            self.out('No releases on the host yet.')
        for name, info in rows:
            what = (f"before deploying {info.get('deploying') or 'a working tree'}: {', '.join(info.get('python') or []) or 'no Python'}"
                    f"{', frontend' if info.get('dist') else ''}") if info.get('kind') == 'deploy' else f"before rolling back to {info.get('restoring', '?')}"
            self.out(f'{name}  {what}')
        return 0


def parse(argv):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('host', help='ssh destination, e.g. alexpi@100.105.170.124')
    parser.add_argument('--dir', default=os.getenv('SIGNAL_DEPLOY_DIR', DEFAULT_DIR), help=f'install dir on the host (default {DEFAULT_DIR})')
    parser.add_argument('--port', type=int, default=8091)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--skip-build', action='store_true')
    parser.add_argument('--skip-tests', action='store_true')
    parser.add_argument('--allow-dirty', action='store_true')
    parser.add_argument('--list', action='store_true')
    parser.add_argument('--rollback', nargs='?', const='latest', metavar='STAMP')
    parser.add_argument('--prune-legacy', action='store_true', help='also delete old dist-before-*, backend-before-*, release-* dirs (asks first)')
    parser.add_argument('--yes', action='store_true', help='do not ask before --prune-legacy')
    parser.add_argument('--verify-seconds', type=int, default=30)
    args = parser.parse_args(argv)
    if args.rollback not in (None, 'latest') and not STAMP.fullmatch(args.rollback):
        parser.error('STAMP looks like 20261102-153000 (see --list)')
    if not os.path.isabs(args.dir) or any(c in args.dir for c in '\n\0'):
        parser.error('--dir must be an absolute path')
    return args


def main(argv=None, remote=None, root=ROOT, out=print, confirm=input):
    args = parse(argv)
    deployer = Deployer(args, remote or SshRemote(args.host), root, out, confirm)
    try:
        if args.list:
            return deployer.list()
        if args.rollback:
            return deployer.rollback()
        if args.prune_legacy and args.dry_run:
            raise DeployError('--prune-legacy deletes; it cannot be combined with --dry-run.', 2)
        return deployer.deploy()
    except DeployError as exc:
        out('FAIL ' + str(exc))
        return exc.code
    except (OSError, subprocess.SubprocessError) as exc:
        out(f'FAIL {type(exc).__name__}: {exc}')
        return 1


if __name__ == '__main__':
    sys.exit(main())
