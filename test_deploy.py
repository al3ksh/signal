"""deploy/deploy.py against a temp 'host' dir: same shell snippets, fake curl/systemctl/sudo on PATH."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('deploy_tool', Path(__file__).resolve().parent / 'deploy' / 'deploy.py')
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)

FAKES = {
    'curl': '''#!/bin/sh
url=""; for a in "$@"; do case "$a" in http*) url="$a";; esac; done
echo "curl $url" >> "$FAKE/calls"
case "$url" in
  */healthz) if grep -q BROKEN "$DEPLOY_DIR/server.py" 2>/dev/null || [ -f "$FAKE/healthz-fail" ]; then exit 22; fi; exit 0;;
  */) cat "$DEPLOY_DIR/dist/index.html";;
esac
''',
    'systemctl': '''#!/bin/sh
echo "systemctl $*" >> "$FAKE/calls"
if [ "$1" = is-active ] && [ -f "$FAKE/inactive" ]; then exit 3; fi
exit 0
''',
    'sudo': '''#!/bin/sh
echo "sudo $*" >> "$FAKE/calls"
[ "$1" = "-n" ] && shift
if [ "$1" = "-l" ]; then [ -f "$FAKE/sudo-refuse" ] && exit 1; exit 0; fi
exec "$@"
''',
    'sleep': '#!/bin/sh\nexit 0\n',
}


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.repo, self.host, self.fake = base / 'repo', base / 'host', base / 'fake'
        for name, text in {'server.py': 'v1\n', 'storage.py': 's1\n', 'alerts.py': 'a1\n', 'runner.py': 'r1\n', 'setup_2fa.py': 't1\n', 'test_server.py': 'test\n',
                           'dist/index.html': '<script src="/assets/index-AAA.js"></script>\n', 'dist/assets/index-AAA.js': 'aaa\n'}.items():
            (self.repo / name).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / name).write_text(text)
        shutil.copytree(self.repo, self.host, ignore=shutil.ignore_patterns('test_*'))
        (self.fake / 'bin').mkdir(parents=True)
        for name, text in FAKES.items():
            path = self.fake / 'bin' / name
            path.write_text(text)
            path.chmod(0o755)
        self.env = {**os.environ, 'PATH': str(self.fake / 'bin') + os.pathsep + os.environ['PATH'], 'FAKE': str(self.fake), 'DEPLOY_DIR': str(self.host)}

    def tearDown(self):
        self.tmp.cleanup()

    def run_deploy(self, *extra, confirm=lambda prompt: 'n'):
        out = []
        code = deploy.main(['alexpi@pi', '--dir', str(self.host), '--skip-build', '--skip-tests', '--allow-dirty', '--verify-seconds', '3', *extra],
                           remote=deploy.LocalRemote(self.env), root=self.repo, out=out.append, confirm=confirm)
        return code, '\n'.join(out)

    def calls(self):
        path = self.fake / 'calls'
        return path.read_text().splitlines() if path.exists() else []

    def restarts(self):
        return [c for c in self.calls() if c.startswith('systemctl restart')]

    def releases(self):
        folder = self.host / 'releases'
        return sorted(p.name for p in folder.iterdir()) if folder.exists() else []

    def new_frontend(self, tag):
        shutil.rmtree(self.repo / 'dist' / 'assets')
        (self.repo / 'dist' / 'assets').mkdir()
        (self.repo / 'dist' / 'assets' / f'index-{tag}.js').write_text(tag + '\n')
        (self.repo / 'dist' / 'index.html').write_text(f'<script src="/assets/index-{tag}.js"></script>\n')

    def tree(self):
        return {str(p.relative_to(self.host)): p.read_bytes() if p.is_file() else None for p in sorted(self.host.rglob('*'))}

    def test_manifest_is_derived_from_files(self):
        manifest = deploy.local_manifest(self.repo)
        self.assertIn('setup_2fa.py', manifest)
        self.assertIn('dist/assets/index-AAA.js', manifest)
        self.assertNotIn('test_server.py', manifest)

    def test_nothing_changed(self):
        code, out = self.run_deploy()
        self.assertEqual(code, 0, out)
        self.assertIn('Nothing to deploy', out)
        self.assertEqual(self.releases(), [])
        self.assertEqual(self.restarts(), [])

    def test_frontend_only_needs_no_restart(self):
        self.new_frontend('BBB')
        code, out = self.run_deploy()
        self.assertEqual(code, 0, out)
        self.assertIn('index-BBB.js', (self.host / 'dist' / 'index.html').read_text())
        self.assertFalse((self.host / 'dist' / 'assets' / 'index-AAA.js').exists())
        self.assertEqual(self.restarts(), [])
        self.assertIn('curl http://127.0.0.1:8091/', self.calls())
        [stamp] = self.releases()
        self.assertTrue((self.host / 'releases' / stamp / 'dist' / 'assets' / 'index-AAA.js').exists())
        self.assertTrue((self.host / 'releases' / stamp / 'manifest.json').exists())
        self.assertFalse(any(p.name.startswith(('dist.old', 'dist-before')) for p in self.host.iterdir()))

    def test_runner_change_restarts_only_the_runner(self):
        (self.repo / 'runner.py').write_text('r2\n')
        code, out = self.run_deploy()
        self.assertEqual(code, 0, out)
        self.assertEqual((self.host / 'runner.py').read_text(), 'r2\n')
        self.assertEqual(self.restarts(), ['systemctl restart signal-runner.service'])
        self.assertIn('systemctl is-active --quiet signal-runner.service', self.calls())

    def test_cli_tools_change_restarts_nothing(self):
        (self.repo / 'setup_2fa.py').write_text('t2\n')
        code, out = self.run_deploy()
        self.assertEqual(code, 0, out)
        self.assertEqual((self.host / 'setup_2fa.py').read_text(), 't2\n')
        self.assertEqual(self.restarts(), [])

    def test_failed_health_restores_the_release_and_exits_non_zero(self):
        (self.repo / 'server.py').write_text('v2 BROKEN\n')
        self.new_frontend('BBB')
        code, out = self.run_deploy()
        self.assertEqual(code, 1)
        self.assertIn('rolled back', out)
        self.assertIn('previous version is running', out)
        self.assertEqual((self.host / 'server.py').read_text(), 'v1\n')
        self.assertIn('index-AAA.js', (self.host / 'dist' / 'index.html').read_text())
        self.assertEqual(self.restarts(), ['systemctl restart signal.service'] * 2)

    def test_pruning_keeps_five_releases(self):
        for i in range(7):
            self.new_frontend(f'R{i}')
            self.assertEqual(self.run_deploy()[0], 0)
        releases = self.releases()
        self.assertEqual(len(releases), 5)
        self.assertTrue(all(deploy.STAMP.fullmatch(name) for name in releases))

    def test_dry_run_touches_nothing(self):
        (self.repo / 'server.py').write_text('v2\n')
        self.new_frontend('BBB')
        before = self.tree()
        code, out = self.run_deploy('--dry-run')
        self.assertEqual(code, 0, out)
        self.assertIn('server.py', out)
        self.assertIn('restart: signal.service', out)
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.calls(), [])

    def test_refused_sudo_changes_nothing_and_prints_the_command(self):
        (self.repo / 'server.py').write_text('v2\n')
        (self.fake / 'sudo-refuse').write_text('')
        before = self.tree()
        code, out = self.run_deploy()
        self.assertEqual(code, 2)
        self.assertIn('sudo systemctl restart signal.service', out)
        self.assertIn('NOPASSWD', out)
        self.assertEqual({k: v for k, v in self.tree().items() if k != '.deploy.lock'}, before)

    def test_rollback_and_back(self):
        self.new_frontend('BBB')
        (self.repo / 'server.py').write_text('v2\n')
        self.assertEqual(self.run_deploy()[0], 0)
        code, out = self.run_deploy('--rollback')
        self.assertEqual(code, 0, out)
        self.assertEqual((self.host / 'server.py').read_text(), 'v1\n')
        self.assertIn('index-AAA.js', (self.host / 'dist' / 'index.html').read_text())
        code, listing = self.run_deploy('--list')
        self.assertEqual(len(listing.splitlines()), 2)
        self.assertIn('before rolling back to', listing)
        code, out = self.run_deploy('--rollback')  # the newest release is the snapshot taken by the rollback
        self.assertEqual(code, 0, out)
        self.assertEqual((self.host / 'server.py').read_text(), 'v2\n')
        self.assertIn('index-BBB.js', (self.host / 'dist' / 'index.html').read_text())
        self.assertEqual(self.run_deploy('--rollback', '19990101-000000')[0], 2)

    def test_prune_legacy_asks_first_and_only_touches_legacy_dirs(self):
        for name in ['dist-before-20260901', 'backend-before-x', 'release-old', 'data']:
            (self.host / name).mkdir()
            (self.host / name / 'f').write_text('x' * 2048)
        code, out = self.run_deploy('--prune-legacy')
        self.assertEqual(code, 0, out)
        self.assertIn('Kept them', out)
        self.assertTrue((self.host / 'release-old').exists())
        code, out = self.run_deploy('--prune-legacy', confirm=lambda prompt: 'y')
        self.assertIn('Removed 3 legacy dirs', out)
        self.assertEqual(sorted(p.name for p in self.host.iterdir() if p.is_dir()), ['data', 'dist'])

    def test_concurrent_deploys_are_refused(self):
        holder = subprocess.Popen(['flock', str(self.host / '.deploy.lock'), 'sleep', '30'])
        try:
            import time
            for _ in range(50):
                if (self.host / '.deploy.lock').exists():
                    break
                time.sleep(0.02)
            time.sleep(0.1)
            self.new_frontend('BBB')
            code, out = self.run_deploy()
            self.assertEqual(code, 1)
            self.assertIn('Another deploy', out)
        finally:
            holder.kill()
            holder.wait()


if __name__ == '__main__':
    unittest.main()
