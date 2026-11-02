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
sub = next((a for a in args if a in ('build', 'pull', 'up', 'stop', 'restart', 'ps', 'config', 'image', 'manifest')), '')
with open(os.path.join(here, 'calls.log'), 'a') as log:
    log.write(json.dumps({'argv': args, 'sub': sub, 'start': time.time(), 'env': {k: os.environ.get(k) for k in ('SIGNAL_COMMIT', 'GIT_TERMINAL_PROMPT')}}) + '\n')
time.sleep(config.get('sleep', {}).get(sub, 0))
with open(os.path.join(here, 'calls.log'), 'a') as log:
    log.write(json.dumps({'end': sub, 'time': time.time()}) + '\n')
if sub == 'ps':
    print(config.get('ps', '{"Service":"web","State":"running","Health":"","ExitCode":0}'))
elif sub == 'config' and '--format' in args:
    print(config.get('configJson', '{"services": {}}'))
elif sub == 'config':
    print(config.get('images', 'demo-web').replace('{commit}', os.environ.get('SIGNAL_COMMIT') or 'none'))
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


def wait(item, job_id, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = item.store.job(job_id)
        if job['status'] not in ('queued', 'running'):
            return job
        time.sleep(0.02)
    raise AssertionError('job did not finish: ' + json.dumps(item.store.job(job_id)))


class LifecycleTests(RunnerCase):
    def test_lifecycle_actions_run_fixed_compose_argv(self):
        fixture = Fixture(self.root, repo=False)
        item = self.make_runner(fixture)
        compose = ['compose', '--project-directory', str(fixture.dir.resolve()), '-p', 'demo', '-f', str((fixture.dir / 'compose.yml').resolve())]
        for action, tail in [('start', ['up', '-d']), ('stop', ['stop']), ('restart', ['restart'])]:
            with self.subTest(action=action):
                job = wait(item, item.submit('demo', action, 'tester')['id'])
                self.assertEqual(job['status'], 'succeeded', job)
                self.assertEqual(job['actor'], 'tester')
                self.assertEqual(fixture.calls()[-2]['argv'], compose + tail)
                log = item.read_log(job['id'], 0)
                self.assertIn('$ docker compose', log['data'])
                self.assertTrue(log['done'])

    def test_failed_command_marks_job_failed(self):
        fixture = Fixture(self.root, repo=False)
        fixture.configure(fail={'restart': 3})
        item = self.make_runner(fixture)
        job = wait(item, item.submit('demo', 'restart', 'tester')['id'])
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(job['exitCode'], 3)
        self.assertIn('fake docker failure', item.read_log(job['id'], 0)['data'])

    def test_rejects_unknown_app_action_and_repo_actions_without_repo(self):
        fixture = Fixture(self.root, repo=False)
        item = self.make_runner(fixture)
        for request, code in [({'op': 'run', 'app': 'nope', 'action': 'start'}, 'not_found'),
                              ({'op': 'run', 'app': 'demo', 'action': 'exec'}, 'invalid'),
                              ({'op': 'run', 'app': 'demo', 'action': 'update'}, 'invalid'),
                              ({'op': 'run', 'app': 'demo', 'action': ['start']}, 'invalid')]:
            with self.subTest(request=request), self.assertRaises(runner.RequestError) as caught:
                item.handle(request)
            self.assertEqual(caught.exception.code, code)
        self.assertEqual(fixture.calls(), [])

    def test_actor_is_sanitised(self):
        fixture = Fixture(self.root, repo=False)
        item = self.make_runner(fixture)
        job = wait(item, item.handle({'op': 'run', 'app': 'demo', 'action': 'start', 'actor': 'x\n$(id)' + 'y' * 200})['job']['id'])
        self.assertEqual(job['actor'], 'x_$(id)' + 'y' * 73)

    def test_one_job_at_a_time_per_app_and_duplicates_refused(self):
        fixture = Fixture(self.root, repo=False)
        fixture.configure(sleep={'restart': 0.4})
        item = self.make_runner(fixture)
        first = item.submit('demo', 'restart', 't')['id']
        with self.assertRaises(runner.RequestError) as caught:
            item.submit('demo', 'restart', 't')
        self.assertEqual(caught.exception.code, 'conflict')
        second = item.submit('demo', 'stop', 't')['id']
        self.assertEqual(item.summary('demo')['activeJob']['id'], first)
        wait(item, first)
        wait(item, second)
        calls = fixture.calls()
        restart_end = next(c['time'] for c in calls if c.get('end') == 'restart')
        stop_start = next(c['start'] for c in calls if c.get('sub') == 'stop')
        self.assertGreaterEqual(stop_start, restart_end)

    def test_cancel_queued_and_running_jobs(self):
        fixture = Fixture(self.root, repo=False)
        fixture.configure(sleep={'restart': 5})
        item = self.make_runner(fixture)
        running = item.submit('demo', 'restart', 't')['id']
        queued = item.submit('demo', 'stop', 't')['id']
        time.sleep(0.3)
        item.cancel(queued)
        item.cancel(running)
        self.assertEqual(wait(item, running, 8)['status'], 'cancelled')
        self.assertEqual(wait(item, queued)['status'], 'cancelled')
        self.assertFalse(any(c.get('sub') == 'stop' for c in fixture.calls()))

    def test_lifecycle_timeout(self):
        fixture = Fixture(self.root, repo=False)
        fixture.configure(sleep={'restart': 5})
        item = self.make_runner(fixture)
        item.timeouts = {**item.timeouts, 'lifecycle': 0.3}
        job = wait(item, item.submit('demo', 'restart', 't')['id'])
        self.assertEqual(job['status'], 'failed')
        self.assertIn('timed out', job['message'])

    def test_log_keeps_head_and_tail_and_is_paged(self):
        fixture = Fixture(self.root, repo=False)
        fixture.configure(output={'restart': '\n'.join(f'line {i:04d} ' + 'x' * 40 for i in range(200))})
        item = self.make_runner(fixture)
        item.log_cap = 2000
        job = wait(item, item.submit('demo', 'restart', 't')['id'])
        size = (item.data_dir / 'logs' / (job['id'] + '.log')).stat().st_size
        self.assertLessEqual(size, 2000)
        log = item.read_log(job['id'], 0)['data']
        self.assertTrue(log.startswith('# restart demo by t'))
        self.assertIn('bytes of output omitted', log)
        self.assertIn('line 0199', log)  # the end of the output, where errors are, survives
        self.assertIn('# succeeded', log)
        self.assertNotIn('line 0100', log)
        first = item.read_log(job['id'], 0, limit=100)
        self.assertEqual(first['offset'], 100)
        self.assertEqual(item.read_log(job['id'], first['offset'], epoch=first['epoch'])['data'], log[100:])

    def test_log_reader_resets_after_compaction(self):
        fixture = Fixture(self.root, repo=False)
        item = self.make_runner(fixture)
        job = wait(item, item.submit('demo', 'restart', 't')['id'])
        stale = item.read_log(job['id'], 40, epoch=3)
        self.assertTrue(stale['reset'])
        self.assertTrue(stale['data'].startswith('# restart'))
        self.assertFalse(item.read_log(job['id'], 40, epoch=stale['epoch'])['reset'])

    def test_history_is_pruned_and_interrupted_jobs_recovered(self):
        fixture = Fixture(self.root, repo=False)
        item = self.make_runner(fixture)
        item.keep_jobs = 3
        ids = [wait(item, item.submit('demo', 'start', 't')['id'])['id'] for _ in range(5)]
        self.assertEqual([j['id'] for j in item.store.jobs('demo', 10)], ids[:1:-1])
        self.assertFalse((item.data_dir / 'logs' / (ids[0] + '.log')).exists())
        with item.store.lock, item.store.db:
            item.store.db.execute("UPDATE jobs SET status='running' WHERE id=?", (ids[-1],))
        item.close()
        self.runners.remove(item)
        again = self.make_runner(fixture)
        self.assertEqual(again.store.job(ids[-1])['status'], 'failed')
        self.assertIn('interrupted', again.store.job(ids[-1])['message'])

    def test_summary_reports_last_deploy_ignoring_checks(self):
        fixture = Fixture(self.root)
        fixture.configure(fail={'restart': 1})
        item = self.make_runner(fixture)
        failed = wait(item, item.submit('demo', 'restart', 't')['id'])
        wait(item, item.submit('demo', 'check', 't')['id'])
        summary = item.summary('demo')
        self.assertEqual(summary['lastJob']['action'], 'check')
        self.assertEqual((summary['lastDeploy']['id'], summary['lastDeploy']['status']), (failed['id'], 'failed'))

    def test_check_job_logs_result(self):
        fixture = Fixture(self.root)
        fixture.push('second')
        item = self.make_runner(fixture)
        job = wait(item, item.submit('demo', 'check', 't')['id'])
        self.assertEqual(job['status'], 'succeeded')
        self.assertIn('1 commit(s) behind', item.read_log(job['id'], 0)['data'])
        self.assertEqual(item.summary('demo')['git']['behind'], 1)


class UpdateTests(RunnerCase):
    def make(self, *fixtures, **kwargs):
        item = self.make_runner(*fixtures, **kwargs)
        item.health_poll = 0.05
        return item

    def subs(self, fixture):
        return [c['sub'] for c in fixture.calls() if 'sub' in c]

    def test_fast_forward_update_builds_deploys_and_records_commits(self):
        fixture = Fixture(self.root)
        item = self.make(fixture)
        prev = fixture.head()
        new = fixture.push('second')
        job = wait(item, item.submit('demo', 'update', 't')['id'])
        self.assertEqual(job['status'], 'succeeded', item.read_log(job['id'], 0)['data'])
        self.assertEqual((job['prevCommit'], job['newCommit'], job['phase']), (prev, new, 'done'))
        self.assertEqual(fixture.head(), new)
        self.assertEqual(self.subs(fixture), ['config', 'image', 'config', 'build', 'up', 'ps'])
        build = next(c for c in fixture.calls() if c.get('sub') == 'build')
        self.assertEqual(build['argv'][-2:], ['build', '--pull'])
        self.assertEqual(build['env']['SIGNAL_COMMIT'], new)
        self.assertEqual(build['env']['GIT_TERMINAL_PROMPT'], '0')
        self.assertIn(['up', '-d', '--remove-orphans'], [c['argv'][-3:] for c in fixture.calls() if c.get('sub') == 'up'])
        self.assertEqual(item.summary('demo')['git']['head'], new)

    def test_already_up_to_date_is_a_no_op(self):
        fixture = Fixture(self.root)
        item = self.make(fixture)
        job = wait(item, item.submit('demo', 'update', 't')['id'])
        self.assertEqual(job['status'], 'succeeded')
        self.assertIn('up to date', job['message'])
        self.assertEqual(self.subs(fixture), [])

    def test_dirty_tree_is_refused(self):
        fixture = Fixture(self.root)
        item = self.make(fixture)
        prev = fixture.head()
        fixture.push('second')
        (fixture.dir / 'web' / 'app.txt').write_text('hand edit on the host\n')
        job = wait(item, item.submit('demo', 'update', 't')['id'])
        self.assertEqual(job['status'], 'failed')
        self.assertIn('local changes', job['message'])
        self.assertEqual(fixture.head(), prev)
        self.assertEqual((fixture.dir / 'web' / 'app.txt').read_text(), 'hand edit on the host\n')
        self.assertEqual(self.subs(fixture), [])

    def test_diverged_branch_is_refused(self):
        fixture = Fixture(self.root)
        item = self.make(fixture)
        fixture.push('upstream change')
        (fixture.dir / 'local.txt').write_text('x\n')
        git(fixture.dir, 'add', 'local.txt')
        git(fixture.dir, 'commit', '-q', '-m', 'local only')
        local = fixture.head()
        job = wait(item, item.submit('demo', 'update', 't')['id'])
        self.assertEqual(job['status'], 'failed')
        self.assertIn('diverged', job['message'])
        self.assertEqual(fixture.head(), local)
        self.assertEqual(self.subs(fixture), [])

    def test_wrong_branch_is_refused(self):
        fixture = Fixture(self.root)
        item = self.make(fixture)
        fixture.push('second')
        git(fixture.dir, 'checkout', '-q', '-b', 'experiment')
        job = wait(item, item.submit('demo', 'update', 't')['id'])
        self.assertEqual(job['status'], 'failed')
        self.assertIn('experiment', job['message'])

    def test_build_failure_rolls_back_to_exact_previous_commit(self):
        fixture = Fixture(self.root)
        fixture.configure(fail={'build': 1})
        item = self.make(fixture)
        prev = fixture.head()
        fixture.push('broken')
        job = wait(item, item.submit('demo', 'update', 't')['id'])
        self.assertEqual(job['status'], 'rolled_back', item.read_log(job['id'], 0)['data'])
        self.assertEqual(fixture.head(), prev)
        self.assertIn(prev[:7], job['message'])
        self.assertEqual(job['phase'], 'rolled back')
        # previous images are re-tagged, so the rollback does not rebuild on the Pi
        self.assertEqual(self.subs(fixture), ['config', 'image', 'config', 'build', 'image', 'up', 'ps'])
        retag = [c['argv'] for c in fixture.calls() if c.get('sub') == 'image']
        self.assertEqual(retag, [['image', 'tag', 'demo-web', 'demo-web:signal-prev'], ['image', 'tag', 'demo-web:signal-prev', 'demo-web']])
        up = [c for c in fixture.calls() if c.get('sub') == 'up'][-1]
        self.assertEqual(up['env']['SIGNAL_COMMIT'], prev)

    def test_rollback_rebuilds_when_previous_images_were_not_saved(self):
        fixture = Fixture(self.root)
        fixture.configure(fail={'up': 1, 'image': 1})
        item = self.make(fixture)
        prev = fixture.head()
        fixture.push('second')
        job = wait(item, item.submit('demo', 'update', 't')['id'])
        self.assertEqual(job['status'], 'failed')  # the rollback's own `up` fails too
        self.assertIn('rollback', job['message'].lower())
        self.assertEqual(fixture.head(), prev)
        self.assertEqual(self.subs(fixture).count('build'), 2)

    def test_health_timeout_rolls_back(self):
        fixture = Fixture(self.root, health={'timeoutSeconds': 1})
        fixture.configure(ps='{"Service":"web","State":"running","Health":"unhealthy","ExitCode":0}')
        item = self.make(fixture)
        prev = fixture.head()
        fixture.push('second')
        job = wait(item, item.submit('demo', 'update', 't')['id'])
        self.assertEqual(job['status'], 'failed')  # still unhealthy after rolling back
        self.assertIn('web is running/unhealthy', job['message'])
        self.assertEqual(fixture.head(), prev)

    def test_http_health_check(self):
        import http.server
        statuses = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(statuses[0] if statuses else 200)
                self.end_headers()
            def log_message(self, *args):
                pass
        httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        fixture = Fixture(self.root, health={'url': f'http://127.0.0.1:{httpd.server_port}/', 'expect': 200, 'timeoutSeconds': 1})
        item = self.make(fixture)
        fixture.push('second')
        self.assertEqual(wait(item, item.submit('demo', 'update', 't')['id'])['status'], 'succeeded')
        statuses.append(503)
        prev = fixture.head()
        fixture.push('third')
        job = wait(item, item.submit('demo', 'update', 't')['id'])
        self.assertEqual(job['status'], 'failed')
        self.assertIn('HTTP 503', job['message'])
        self.assertEqual(fixture.head(), prev)

    def test_exited_one_shot_containers_are_healthy(self):
        fixture = Fixture(self.root, health={'timeoutSeconds': 1})
        fixture.configure(ps='{"Service":"web","State":"running","Health":"healthy","ExitCode":0}\n{"Service":"migrate","State":"exited","Health":"","ExitCode":0}')
        item = self.make(fixture)
        fixture.push('second')
        self.assertEqual(wait(item, item.submit('demo', 'update', 't')['id'])['status'], 'succeeded')

    def test_compose_ps_array_output(self):
        fixture = Fixture(self.root, health={'timeoutSeconds': 1})
        fixture.configure(ps='[{"Service":"web","State":"exited","Health":"","ExitCode":137}]')
        item = self.make(fixture)
        fixture.push('second')
        job = wait(item, item.submit('demo', 'update', 't')['id'])
        self.assertIn('web is exited', job['message'])

    def test_manual_rollback_returns_to_commit_before_last_successful_update(self):
        fixture = Fixture(self.root)
        item = self.make(fixture)
        first = fixture.head()
        fixture.push('second')
        self.assertIsNone(item.summary('demo')['rollbackTo'])
        self.assertEqual(wait(item, item.submit('demo', 'update', 't')['id'])['status'], 'succeeded')
        self.assertEqual(item.summary('demo')['rollbackTo'], first[:7])
        self.assertIsNone(item.summary('demo')['activeJob'])
        job = wait(item, item.submit('demo', 'rollback', 't')['id'])
        self.assertEqual(job['status'], 'succeeded', item.read_log(job['id'], 0)['data'])
        self.assertEqual(fixture.head(), first)
        self.assertEqual(job['newCommit'], first)
        # the update's saved images are restored instead of rebuilding
        self.assertEqual(self.subs(fixture)[-5:], ['config', 'image', 'image', 'up', 'ps'])
        self.assertEqual(self.subs(fixture).count('build'), 1)

    def test_rollback_without_history_is_refused(self):
        fixture = Fixture(self.root)
        item = self.make(fixture)
        job = wait(item, item.submit('demo', 'rollback', 't')['id'])
        self.assertEqual(job['status'], 'failed')
        self.assertIn('No successful update', job['message'])

    def test_cancelled_build_rolls_back(self):
        fixture = Fixture(self.root)
        fixture.configure(sleep={'build': 5})
        item = self.make(fixture)
        prev = fixture.head()
        fixture.push('second')
        job_id = item.submit('demo', 'update', 't')['id']
        deadline = time.time() + 5
        while time.time() < deadline and item.store.job(job_id)['phase'] != 'build':
            time.sleep(0.02)
        time.sleep(0.2)
        item.cancel(job_id)
        job = wait(item, job_id)
        self.assertEqual(job['status'], 'rolled_back')
        self.assertEqual(fixture.head(), prev)

    def test_global_build_limit(self):
        one, two = Fixture(self.root, app_id='one'), Fixture(self.root, app_id='two')
        one.configure(sleep={'build': 0.5})
        item = self.make(one, two)
        one.push('a')
        two.push('b')
        jobs = [item.submit('one', 'update', 't')['id'], item.submit('two', 'update', 't')['id']]
        for job_id in jobs:
            self.assertEqual(wait(item, job_id)['status'], 'succeeded')
        calls = one.calls()  # both fixtures share bin/calls.log
        builds = [c for c in calls if c.get('sub') == 'build']
        ends = [c['time'] for c in calls if c.get('end') == 'build']
        self.assertEqual(len(builds), 2)
        self.assertGreaterEqual(builds[1]['start'], ends[0])


class RecoveryTests(RunnerCase):
    def interrupted(self, fixture, phase, move=True):
        """Leave the state a runner killed mid-deploy would leave: a running job and a moved checkout."""
        item = self.make_runner(fixture)
        prev = fixture.head()
        new = fixture.push('second')
        if move:
            git(fixture.dir, 'fetch', '-q', 'origin')
            git(fixture.dir, 'merge', '-q', '--ff-only', 'origin/main')
        job = {'id': 'a' * 32, 'app': 'demo', 'action': 'update', 'actor': 't', 'created': time.time()}
        item.store.create(job)
        item.store.update(job['id'], status='running', phase=phase, started=time.time(), prev_commit=prev,
                          new_commit=new if move else None, images=json.dumps([['demo-web', 'demo-web:signal-prev']]))
        item.close()
        self.runners.remove(item)
        return prev, new, job['id']

    def recovery_jobs(self, item):
        return [j for j in item.store.jobs('demo', 10) if j['action'] == 'recover']

    def test_interrupted_deploy_is_rolled_back_on_start(self):
        fixture = Fixture(self.root)
        prev, new, old_id = self.interrupted(fixture, 'health')
        self.assertEqual(fixture.head(), new)
        item = self.make_runner(fixture)
        item.health_poll = 0.05
        jobs = self.recovery_jobs(item)
        self.assertEqual(len(jobs), 1)
        job = wait(item, jobs[0]['id'])
        self.assertEqual(job['status'], 'rolled_back', item.read_log(job['id'], 0)['data'])
        self.assertEqual(job['actor'], 'runner (recovery)')
        self.assertEqual(fixture.head(), prev)
        self.assertEqual((job['prevCommit'], job['newCommit']), (new, prev))
        old = item.store.job(old_id)
        self.assertEqual(old['status'], 'failed')
        self.assertIn(job['id'][:12], old['message'])
        calls = fixture.calls()
        self.assertIn(['image', 'tag', 'demo-web:signal-prev', 'demo-web'], [c['argv'] for c in calls if c.get('sub') == 'image'])
        self.assertEqual([c for c in calls if c.get('sub') == 'up'][-1]['env']['SIGNAL_COMMIT'], prev)

    def test_interrupted_before_the_checkout_moved_needs_nothing(self):
        fixture = Fixture(self.root)
        prev, _, old_id = self.interrupted(fixture, 'fetch', move=False)
        item = self.make_runner(fixture)
        self.assertEqual(self.recovery_jobs(item), [])
        self.assertEqual(item.store.job(old_id)['status'], 'failed')
        self.assertEqual(fixture.head(), prev)

    def test_checkout_changed_by_hand_is_left_alone(self):
        fixture = Fixture(self.root)
        _, new, _ = self.interrupted(fixture, 'build')
        (fixture.dir / 'hand.txt').write_text('x\n')
        git(fixture.dir, 'add', 'hand.txt')
        git(fixture.dir, 'commit', '-q', '-m', 'hand fix on the host')
        manual = fixture.head()
        item = self.make_runner(fixture)
        self.assertEqual(self.recovery_jobs(item), [])
        self.assertEqual(fixture.head(), manual)

    def test_recover_is_not_a_requestable_action(self):
        fixture = Fixture(self.root)
        item = self.make_runner(fixture)
        with self.assertRaises(runner.RequestError):
            item.handle({'op': 'run', 'app': 'demo', 'action': 'recover'})


class SpeedTests(RunnerCase):
    def make(self, *fixtures):
        item = self.make_runner(*fixtures)
        item.health_poll = 0.05
        return item

    def subs(self, fixture):
        return [c['sub'] for c in fixture.calls() if 'sub' in c]

    def test_pull_mode_pulls_ci_images_for_the_new_commit(self):
        fixture = Fixture(self.root, mode='pull')
        fixture.configure(images='ghcr.io/al3ksh/demo:abc')
        item = self.make(fixture)
        new = fixture.push('second')
        job = wait(item, item.submit('demo', 'update', 't')['id'])
        self.assertEqual(job['status'], 'succeeded', item.read_log(job['id'], 0)['data'])
        self.assertEqual(self.subs(fixture), ['config', 'config', 'config', 'image', 'pull', 'up', 'ps'])  # fixed tag: nothing to pre-check
        pull = next(c for c in fixture.calls() if c.get('sub') == 'pull')
        self.assertEqual(pull['env']['SIGNAL_COMMIT'], new)
        self.assertEqual(next(c for c in fixture.calls() if c.get('sub') == 'image')['argv'], ['image', 'tag', 'ghcr.io/al3ksh/demo:abc', 'ghcr.io/al3ksh/demo:signal-prev'])

    def test_pull_failure_restores_previous_images_without_building(self):
        fixture = Fixture(self.root, mode='pull')
        fixture.configure(fail={'pull': 1})
        item = self.make(fixture)
        prev = fixture.head()
        fixture.push('second')
        job = wait(item, item.submit('demo', 'update', 't')['id'])
        self.assertEqual(job['status'], 'rolled_back')
        self.assertEqual(fixture.head(), prev)
        self.assertNotIn('build', self.subs(fixture))
        self.assertEqual(self.subs(fixture)[-3:], ['image', 'up', 'ps'])

    def test_pull_mode_refuses_before_ci_published_the_image(self):
        fixture = Fixture(self.root, mode='pull')
        fixture.configure(images='ghcr.io/al3ksh/demo:{commit}', fail={'manifest': 1})
        item = self.make(fixture)
        prev = fixture.head()
        new = fixture.push('second')
        job = wait(item, item.submit('demo', 'update', 't')['id'])
        self.assertEqual(job['status'], 'failed')
        self.assertIn('ghcr.io/al3ksh/demo:' + new, job['message'])
        self.assertIn('not published yet', job['message'])
        self.assertEqual(fixture.head(), prev)
        self.assertNotIn('pull', self.subs(fixture))
        self.assertNotIn('image', self.subs(fixture))

    def test_pull_mode_checks_the_commit_pinned_image(self):
        fixture = Fixture(self.root, mode='pull')
        fixture.configure(images='ghcr.io/al3ksh/demo:{commit}\npostgres:16')
        item = self.make(fixture)
        new = fixture.push('second')
        self.assertEqual(wait(item, item.submit('demo', 'update', 't')['id'])['status'], 'succeeded')
        self.assertEqual([c['argv'] for c in fixture.calls() if c.get('sub') == 'manifest'], [['manifest', 'inspect', 'ghcr.io/al3ksh/demo:' + new]])

    def two_service_fixture(self):
        fixture = Fixture(self.root)
        config = {'services': {
            'web': {'build': {'context': str((fixture.dir / 'web').resolve()), 'dockerfile': 'Dockerfile'}},
            'api': {'build': {'context': str((fixture.dir / 'api').resolve()), 'dockerfile': str((fixture.dir / 'docker' / 'api.Dockerfile').resolve())}},
            'db': {'image': 'postgres:16'}}}
        fixture.configure(configJson=json.dumps(config))
        return fixture

    def build_args(self, fixture):
        return [c['argv'][c['argv'].index('build'):] for c in fixture.calls() if c.get('sub') == 'build']

    def test_build_only_services_whose_context_changed(self):
        fixture = self.two_service_fixture()
        item = self.make(fixture)
        fixture.push('web change', 'web/index.html')
        self.assertEqual(wait(item, item.submit('demo', 'update', 't')['id'])['status'], 'succeeded')
        self.assertEqual(self.build_args(fixture), [['build', '--pull', 'web']])

    def test_dockerfile_outside_context_counts(self):
        fixture = self.two_service_fixture()
        item = self.make(fixture)
        fixture.push('api dockerfile', 'docker/api.Dockerfile')
        self.assertEqual(wait(item, item.submit('demo', 'update', 't')['id'])['status'], 'succeeded')
        self.assertEqual(self.build_args(fixture), [['build', '--pull', 'api']])

    def test_unrelated_change_skips_the_build(self):
        fixture = self.two_service_fixture()
        item = self.make(fixture)
        fixture.push('docs', 'README.md')
        job = wait(item, item.submit('demo', 'update', 't')['id'])
        self.assertEqual(job['status'], 'succeeded')
        self.assertEqual(self.build_args(fixture), [])
        self.assertIn('skipping the build', item.read_log(job['id'], 0)['data'])
        self.assertIn('up', self.subs(fixture))

    def test_compose_file_change_builds_everything(self):
        fixture = self.two_service_fixture()
        item = self.make(fixture)
        fixture.push('compose', 'compose.yml', 'services: {web: {build: ./web}}\n')
        self.assertEqual(wait(item, item.submit('demo', 'update', 't')['id'])['status'], 'succeeded')
        self.assertEqual(self.build_args(fixture), [['build', '--pull']])

    def test_unreadable_config_builds_everything(self):
        fixture = Fixture(self.root)
        fixture.configure(configJson='not json')
        item = self.make(fixture)
        fixture.push('web change')
        self.assertEqual(wait(item, item.submit('demo', 'update', 't')['id'])['status'], 'succeeded')
        self.assertEqual(self.build_args(fixture), [['build', '--pull']])

    def test_rendered_config_is_not_logged(self):
        fixture = Fixture(self.root)
        config = {'services': {'web': {'build': {'context': str((fixture.dir / 'web').resolve())}, 'environment': {'TOKEN': 'hunter2-secret'}}}}
        fixture.configure(configJson=json.dumps(config))
        item = self.make(fixture)
        fixture.push('web change')
        job = wait(item, item.submit('demo', 'update', 't')['id'])
        self.assertEqual(job['status'], 'succeeded')
        self.assertNotIn('hunter2-secret', item.read_log(job['id'], 0)['data'])

    def test_option_like_service_names_are_never_passed(self):
        fixture = Fixture(self.root)
        fixture.configure(configJson=json.dumps({'services': {'--privileged': {'build': {'context': str((fixture.dir / 'web').resolve())}}}}))
        item = self.make(fixture)
        fixture.push('web change')
        self.assertEqual(wait(item, item.submit('demo', 'update', 't')['id'])['status'], 'succeeded')
        self.assertEqual(self.build_args(fixture), [['build', '--pull']])


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
