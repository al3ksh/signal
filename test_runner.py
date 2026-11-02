"""Runner tests against temporary git repositories and a fake docker executable. No real Docker is used."""
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import runner

FAKE_DOCKER = r'''#!/usr/bin/env python3
"""Records argv; behaviour comes from fake.json next to this file."""
import json, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
try:
    config = json.load(open(os.path.join(here, 'fake.json')))
except (OSError, ValueError):
    config = {}
args = sys.argv[1:]
sub = next((a for a in args if a in ('build', 'pull', 'up', 'stop', 'restart', 'ps', 'config', 'image')), '')
with open(os.path.join(here, 'calls.log'), 'a') as log:
    log.write(json.dumps({'argv': args, 'sub': sub, 'start': time.time(), 'env': {k: os.environ.get(k) for k in ('SIGNAL_COMMIT', 'GIT_TERMINAL_PROMPT')}}) + '\n')
time.sleep(config.get('sleep', {}).get(sub, 0))
with open(os.path.join(here, 'calls.log'), 'a') as log:
    log.write(json.dumps({'end': sub, 'time': time.time()}) + '\n')
if sub == 'ps':
    print(config.get('ps', '{"Service":"web","State":"running","Health":"","ExitCode":0}'))
elif sub == 'config':
    print(config.get('images', 'demo-web'))
elif sub in config.get('output', {}):
    print(config['output'][sub])
fail = config.get('fail', {})
if sub in fail:
    print('fake docker failure in ' + sub, file=sys.stderr)
    sys.exit(fail[sub])
'''


def git(cwd, *args):
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t'}).stdout.strip()


class Fixture:
    """A bare remote, a deployed clone in apps/<id>, an upstream working clone and a fake docker."""
    def __init__(self, root, app_id='demo', repo=True, health=None, mode='build', compose='compose.yml'):
        self.root = Path(root)
        self.bin = self.root / 'bin'
        self.bin.mkdir(exist_ok=True)
        docker = self.bin / 'docker'
        docker.write_text(FAKE_DOCKER)
        docker.chmod(0o755)
        self.app_id = app_id
        self.dir = self.root / 'apps' / app_id
        self.remote = self.root / 'remotes' / (app_id + '.git')
        self.upstream = self.root / 'upstream' / app_id
        if repo:
            self.remote.mkdir(parents=True)
            git(self.remote, 'init', '--bare', '-q', '-b', 'main')
            self.upstream.mkdir(parents=True)
            git(self.upstream, 'init', '-q', '-b', 'main')
            (self.upstream / compose).write_text('services: {web: {build: .}}\n')
            (self.upstream / 'web').mkdir()
            (self.upstream / 'web' / 'app.txt').write_text('v1\n')
            git(self.upstream, 'add', '.')
            git(self.upstream, 'commit', '-q', '-m', 'first')
            git(self.upstream, 'remote', 'add', 'origin', str(self.remote))
            git(self.upstream, 'push', '-q', 'origin', 'main')
            self.dir.parent.mkdir(parents=True, exist_ok=True)
            git(self.dir.parent, 'clone', '-q', str(self.remote), app_id)
        else:
            self.dir.mkdir(parents=True)
            (self.dir / compose).write_text('services: {web: {image: demo}}\n')
        entry = {'id': app_id, 'name': app_id.title(), 'dir': str(self.dir), 'composeFiles': [compose],
                 'update': {'mode': mode}}
        if repo:
            entry['repo'] = {'remote': 'origin', 'branch': 'main'}
        if health:
            entry['health'] = health
        self.entry = entry

    def push(self, message, path='web/app.txt', text=None):
        target = self.upstream / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text if text is not None else message + '\n')
        git(self.upstream, 'add', '.')
        git(self.upstream, 'commit', '-q', '-m', message)
        git(self.upstream, 'push', '-q', 'origin', 'main')
        return git(self.upstream, 'rev-parse', 'HEAD')

    def head(self):
        return git(self.dir, 'rev-parse', 'HEAD')

    def configure(self, **config):
        (self.bin / 'fake.json').write_text(json.dumps(config))

    def calls(self):
        path = self.bin / 'calls.log'
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class RunnerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.runners = []

    def tearDown(self):
        for item in self.runners:
            item.close()
        self.tmp.cleanup()

    def make_runner(self, *fixtures, check_interval=0):
        registry = self.root / 'apps.json'
        registry.write_text(json.dumps({'apps': [f.entry for f in fixtures]}))
        item = runner.Runner(runner.load_registry(registry), self.root / 'state', path=str(self.root / 'bin') + os.pathsep + os.environ['PATH'], check_interval=check_interval)
        self.runners.append(item)
        return item


class RegistryTests(RunnerCase):
    def write(self, apps):
        path = self.root / 'apps.json'
        path.write_text(json.dumps({'apps': apps}))
        return path

    def test_valid_registry_resolves_compose_files(self):
        fixture = Fixture(self.root, repo=False)
        apps = runner.load_registry(self.write([fixture.entry]))
        self.assertEqual(apps['demo']['composeFiles'], [str((fixture.dir / 'compose.yml').resolve())])
        self.assertIsNone(apps['demo']['repo'])

    def test_rejects_bad_id(self):
        fixture = Fixture(self.root, repo=False)
        for bad in ['Demo', 'a b', '', 'x' * 41, '../etc', 'a;rm']:
            with self.subTest(bad=bad), self.assertRaises(runner.RegistryError):
                runner.load_registry(self.write([{**fixture.entry, 'id': bad}]))

    def test_rejects_path_escape_and_missing_compose_file(self):
        fixture = Fixture(self.root, repo=False)
        (self.root / 'apps' / 'outside.yml').write_text('services: {}\n')
        for files in [['../outside.yml'], ['missing.yml'], [str(self.root / 'apps' / 'outside.yml')], []]:
            with self.subTest(files=files), self.assertRaises(runner.RegistryError):
                runner.load_registry(self.write([{**fixture.entry, 'composeFiles': files}]))

    def test_rejects_relative_or_missing_dir_and_duplicates(self):
        fixture = Fixture(self.root, repo=False)
        for entry in [{**fixture.entry, 'dir': 'apps/demo'}, {**fixture.entry, 'dir': str(self.root / 'nope')}]:
            with self.subTest(entry=entry['dir']), self.assertRaises(runner.RegistryError):
                runner.load_registry(self.write([entry]))
        with self.assertRaises(runner.RegistryError):
            runner.load_registry(self.write([fixture.entry, fixture.entry]))

    def test_rejects_option_like_git_refs_and_bad_mode(self):
        fixture = Fixture(self.root, repo=False)
        for repo in [{'remote': '--upload-pack=x', 'branch': 'main'}, {'remote': 'origin', 'branch': '-x'}, {'remote': 'origin', 'branch': 'a..b'}]:
            with self.subTest(repo=repo), self.assertRaises(runner.RegistryError):
                runner.load_registry(self.write([{**fixture.entry, 'repo': repo}]))
        with self.assertRaises(runner.RegistryError):
            runner.load_registry(self.write([{**fixture.entry, 'update': {'mode': 'yolo'}}]))

    def test_example_registry_is_well_formed(self):
        data = json.loads((Path(runner.__file__).parent / 'apps.example.json').read_text())
        ids = [app['id'] for app in data['apps']]
        self.assertIn('portfolio', ids)
        self.assertTrue(all(runner.APP_ID.fullmatch(i) for i in ids))


class CheckTests(RunnerCase):
    def test_check_reports_commits_behind(self):
        fixture = Fixture(self.root)
        item = self.make_runner(fixture)
        fixture.push('second')
        fixture.push('third')
        result = item.inspect('demo', fetch=True)
        self.assertEqual(result['behind'], 2)
        self.assertEqual(result['ahead'], 0)
        self.assertEqual([c['subject'] for c in result['commits']], ['third', 'second'])
        self.assertEqual(result['head'], fixture.head())
        self.assertFalse(result['dirty'])
        self.assertEqual(result['branch'], 'main')
        self.assertEqual(item.summary('demo')['git']['behind'], 2)

    def test_check_reports_dirty_tree(self):
        fixture = Fixture(self.root)
        item = self.make_runner(fixture)
        (fixture.dir / 'web' / 'app.txt').write_text('local edit\n')
        self.assertTrue(item.inspect('demo', fetch=True)['dirty'])

    def test_summary_hides_paths(self):
        fixture = Fixture(self.root)
        item = self.make_runner(fixture)
        summary = json.dumps(item.summary('demo'))
        self.assertNotIn(str(self.root), summary)

    def test_app_without_repo_has_no_git(self):
        fixture = Fixture(self.root, repo=False)
        item = self.make_runner(fixture)
        self.assertFalse(item.summary('demo')['hasRepo'])
        self.assertIsNone(item.summary('demo')['git'])

    def test_periodic_check_runs(self):
        fixture = Fixture(self.root)
        fixture.push('second')
        item = self.make_runner(fixture, check_interval=1)
        item.start_checker(initial_delay=0)
        deadline = time.time() + 10
        while time.time() < deadline and (item.summary('demo')['git'] or {}).get('behind') != 1:
            time.sleep(0.05)
        self.assertEqual(item.summary('demo')['git']['behind'], 1)


class SocketTests(RunnerCase):
    def test_list_status_and_errors_over_unix_socket(self):
        fixture = Fixture(self.root)
        item = self.make_runner(fixture)
        path = str(self.root / 'control.sock')
        server = runner.serve(item, path)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), '0o660')

        def call(payload):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.connect(path)
                sock.sendall((payload if isinstance(payload, bytes) else json.dumps(payload).encode()) + b'\n')
                return json.loads(sock.makefile('rb').readline())
        listed = call({'op': 'list'})
        self.assertTrue(listed['ok'])
        self.assertEqual(listed['apps'][0]['id'], 'demo')
        self.assertEqual(call({'op': 'status', 'app': 'demo'})['app']['name'], 'Demo')
        self.assertEqual(call({'op': 'status', 'app': 'nope'})['code'], 'not_found')
        self.assertEqual(call({'op': 'exec', 'cmd': 'id'})['code'], 'invalid')
        self.assertEqual(call(b'not json')['code'], 'invalid')


if __name__ == '__main__':
    unittest.main()
