"""deploy/pi-clean.py with canned docker/system output and a temp filesystem tree. Nothing real is touched."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

spec = importlib.util.spec_from_file_location('pi_clean', Path(__file__).resolve().parent / 'deploy' / 'pi-clean.py')
clean = importlib.util.module_from_spec(spec)
spec.loader.exec_module(clean)

NOW = time.time()
GB, MB = 10 ** 9, 10 ** 6
MUTATING = (('docker', 'image', 'rm'), ('docker', 'image', 'prune'), ('docker', 'builder', 'prune'), ('docker', 'rm'), ('journalctl', '--vacuum-size=100M'),
            ('apt-get', 'clean'), ('systemctl', 'restart'))


def iso(days_ago):
    return time.strftime('%Y-%m-%dT%H:%M:%S.123456789Z', time.gmtime(NOW - days_ago * 86400))


def image(short, tags, size, days=30):
    return {'Id': 'sha256:' + short * 64, 'RepoTags': tags, 'Size': size, 'Created': iso(days)}


def container(name, image_id, status, days, project=None):
    labels = {'com.docker.compose.project': project} if project else {}
    return {'Id': name.replace('-', '') + 'c' * 20, 'Name': '/' + name, 'Image': image_id, 'Config': {'Image': '?', 'Labels': labels},
            'State': {'Status': status, 'FinishedAt': iso(days) if status != 'running' else '0001-01-01T00:00:00Z'}}


class FakeDocker:
    def __init__(self):
        self.images = [image('a', ['ghcr.io/al3ksh/tools:main'], 300 * MB), image('b', ['lscr.io/linuxserver/chromium:latest'], 4500 * MB),
                       image('c', ['postgres:16'], 400 * MB), image('d', ['medium-server:signal-prev'], 200 * MB),
                       image('e', ['oldapp-web:signal-prev'], 150 * MB), image('f', ['nginx:1.25'], 70 * MB),
                       image('9', ['<none>:<none>'], 90 * MB), image('8', ['ai-proxy:latest'], 50 * MB),
                       image('7', ['medium-server:latest'], 220 * MB), image('6', ['medium-db:latest'], 120 * MB)]
        ids = {i['RepoTags'][0]: i['Id'] for i in self.images}
        self.containers = [container('tools-web', ids['ghcr.io/al3ksh/tools:main'], 'running', 0, 'tools'),
                           container('ai-browser', ids['lscr.io/linuxserver/chromium:latest'], 'exited', 45, 'ai-browser'),
                           container('ai-browser-proxy', ids['ai-proxy:latest'], 'exited', 45, 'ai-browser'),
                           container('medium-server', ids['medium-server:latest'], 'exited', 40, 'medium'),
                           container('medium-db', ids['medium-db:latest'], 'running', 0, 'medium'),
                           container('scratch', ids['nginx:1.25'], 'exited', 2)]
        self.containers[-1]['Image'] = 'sha256:' + '5' * 8  # recent exit of an image that no longer exists
        self.calls = []
        self.fail = set()

    def __call__(self, argv, timeout=120):
        self.calls.append(argv)
        key = tuple(argv)
        if any(key[:len(m)] == m for m in MUTATING) or (key[:2] == ('docker', 'compose') and 'down' in key):
            return (1, 'boom') if key in self.fail else (0, '')
        if key[:4] == ('docker', 'image', 'ls', '-q'):
            return 0, '\n'.join(i['Id'] for i in self.images)
        if key[:3] == ('docker', 'image', 'inspect'):
            return 0, json.dumps([i for i in self.images if i['Id'] in key[3:]])
        if key[:3] == ('docker', 'ps', '-aq'):
            return 0, '\n'.join(c['Id'] for c in self.containers)
        if key[:3] == ('docker', 'container', 'inspect'):
            return 0, json.dumps([c for c in self.containers if c['Id'] in key[3:]])
        if key[:2] == ('docker', 'compose') and key[-2:] == ('config', '--images'):
            return 0, {'tools': 'ghcr.io/al3ksh/tools:main', 'medium': 'docker.io/library/postgres:16\nmedium-server'}.get(key[key.index('-p') + 1], '')
        if key[:3] == ('docker', 'system', 'df'):
            return 0, '{"Type":"Images","Size":"10GB","Reclaimable":"9.9GB (99%)"}\n{"Type":"Build Cache","Size":"1.2GB","Reclaimable":"1.2GB"}'
        if key[:3] == ('docker', 'volume', 'ls'):
            return 0, 'medium_old_data\n'
        if key == ('journalctl', '--disk-usage'):
            return 0, 'Archived and active journals take up 121.0M in the file system.'
        if key[0] == 'find':
            return 0, ''
        return 127, 'unexpected: ' + ' '.join(argv)

    def mutations(self):
        return [c for c in self.calls if any(tuple(c[:len(m)]) == m for m in MUTATING) or (c[:2] == ['docker', 'compose'] and 'down' in c)]


class CleanerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.write('etc/signal/apps.json', json.dumps({'apps': [
            {'id': 'tools', 'dir': '/home/alexpi/apps/tools', 'composeFiles': ['docker-compose.yml']},
            {'id': 'medium', 'dir': '/home/alexpi/apps/Medium', 'composeFiles': ['docker-compose.prod.yml']}]}))
        self.write('etc/systemd/journald.conf', '[Journal]\n#SystemMaxUse=\n')
        self.write('etc/default/atop', 'LOGOPTS=""\nLOGGENERATIONS=28\n')
        self.write('var/log/atop/atop_20260901', 'x' * 5000, age_days=60)
        self.write('var/log/atop/atop_20261101', 'x' * 5000, age_days=1)
        self.write('var/cache/apt/archives/vim_9.deb', 'x' * 3000)
        signal = 'home/alexpi/apps/signal'
        for i, name in enumerate(['dist-before-1', 'dist-before-2', 'backend-before-3', 'release-4', 'dist-before-5', 'dist-before-6', 'release-7']):
            self.write(f'{signal}/{name}/index.html', 'x' * 100, age_days=30 - i)
        self.write(f'{signal}/server.py', 'x')
        self.write('home/alexpi/apps/tools/app/__pycache__/m.cpython-311.pyc', 'x' * 400)
        self.write('home/alexpi/backups/photos.tar', 'x' * 7000, age_days=100)
        self.write('home/alexpi/data/notes.txt', 'keep me')
        self.docker = FakeDocker()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, path, text, age_days=0):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        stamp = NOW - age_days * 86400
        os.utime(target, (stamp, stamp))
        if target.parent.name not in ('atop', 'archives'):
            os.utime(target.parent, (stamp, stamp))

    def scanner(self):
        return clean.Scanner(root=self.root, home='/home/alexpi', run=self.docker, now=NOW)

    def findings(self):
        return {f['id']: f for f in self.scanner().scan()}

    def run_main(self, *argv, answers=()):
        out, answers = [], list(answers)
        code = clean.main(list(argv), scanner=self.scanner(), out=out.append, ask=lambda prompt: answers.pop(0) if answers else 'n')
        return code, '\n'.join(out)

    def test_scan_changes_nothing(self):
        before = {str(p): p.stat().st_mtime for p in self.root.rglob('*')}
        code, out = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn('Nothing was changed', out)
        self.assertEqual(self.docker.mutations(), [])
        self.assertEqual({str(p): p.stat().st_mtime for p in self.root.rglob('*')}, before)
        code, out = self.run_main('--json')
        self.assertTrue(json.loads(out)['findings'])
        self.assertEqual(self.docker.mutations(), [])

    def test_unused_images_come_from_references_not_docker_df(self):
        found = self.findings()
        unused = {f['title'] for f in found.values() if f['category'] == 'docker-unused-image'}
        self.assertEqual(unused, {'oldapp-web:signal-prev', 'nginx:1.25'})  # docker df claims 99 % reclaimable; it is not
        self.assertEqual(found['image:' + 'f' * 12]['action'], [{'argv': ['docker', 'image', 'rm', 'nginx:1.25']}])
        self.assertEqual(found['image:' + 'f' * 12]['safety'], 'review')

    def test_signal_prev_of_registered_app_is_never_proposed(self):
        titles = [f['title'] for f in self.findings().values()]
        self.assertNotIn('medium-server:signal-prev', titles)
        self.docker.images.append(image('4', ['discord-bot-bot:signal-prev'], 10 * MB))  # build-mode name: <project>-<service>
        self.write('etc/signal/apps.json', json.dumps({'apps': [{'id': 'discord-bot', 'dir': '/x', 'composeFiles': []}]}))
        self.assertNotIn('discord-bot-bot:signal-prev', [f['title'] for f in self.findings().values()])

    def test_compose_referenced_image_is_kept(self):
        self.assertNotIn('postgres:16', [f['title'] for f in self.findings().values()])

    def test_stopped_container_pins_its_image_until_it_goes(self):
        found = self.findings()
        self.assertNotIn('lscr.io/linuxserver/chromium:latest', [f['title'] for f in found.values()])
        project = found['project:ai-browser']
        self.assertEqual((project['bytes'], project['safety']), (4550 * MB, 'review'))
        self.assertEqual(project['action'], [{'argv': ['docker', 'compose', '-p', 'ai-browser', 'down']}])
        self.assertIn('chromium', project['reason'])
        # medium still has a running container: only the stopped one goes, never `compose down`
        self.assertEqual(found['container:medium-server']['action'], [{'argv': ['docker', 'rm', self.docker.containers[3]['Id']]}])
        self.assertNotIn('container:scratch', found)  # exited two days ago
        self.docker.containers = [c for c in self.docker.containers if not c['Name'].startswith('/ai-browser')]
        unused = {f['title'] for f in self.findings().values() if f['category'] == 'docker-unused-image'}
        self.assertIn('lscr.io/linuxserver/chromium:latest', unused)

    def test_volumes_are_only_reported(self):
        volume = self.findings()['volume:medium_old_data']
        self.assertEqual((volume['safety'], volume['action']), ('report-only', []))
        self.run_main('apply', answers=['y'] * 20)
        self.assertFalse(any('volume' in c for c in self.docker.mutations()))

    def test_system_findings(self):
        found = self.findings()
        self.assertEqual(found['journal']['bytes'], int(121 * 1024 ** 2) - 100 * 1024 ** 2)
        self.assertEqual(found['journal-limit']['safety'], 'review')
        self.assertEqual(found['atop']['action'], [{'delete': ['/var/log/atop/atop_20260901']}])
        self.assertEqual(found['atop-limit']['action'][0]['content'], 'LOGOPTS=""\nLOGGENERATIONS=7\n')
        self.assertEqual(found['apt']['action'], [{'argv': ['apt-get', 'clean']}])
        self.assertEqual(len(found['signal-releases']['action'][0]['delete']), 2)  # 7 backups, newest 5 kept
        self.assertEqual(found['backup:backups/photos.tar']['safety'], 'report-only')
        self.assertEqual(found['docker-build-cache']['bytes'], int(1.2 * GB))

    def test_apply_safe_runs_safe_items_only(self):
        code, out = self.run_main('apply', '--safe')
        self.assertEqual(code, 0)
        self.assertEqual(sorted(' '.join(c) for c in self.docker.mutations()),
                         ['apt-get clean', 'docker builder prune -f', 'docker image prune -f', 'journalctl --vacuum-size=100M'])
        self.assertFalse((self.root / 'var/log/atop/atop_20260901').exists())
        self.assertTrue((self.root / 'var/log/atop/atop_20261101').exists())
        self.assertFalse((self.root / 'home/alexpi/apps/signal/dist-before-1').exists())
        self.assertTrue((self.root / 'home/alexpi/apps/signal/release-7').exists())
        self.assertFalse((self.root / 'home/alexpi/apps/tools/app/__pycache__').exists())
        self.assertTrue((self.root / 'home/alexpi/backups/photos.tar').exists())
        self.assertTrue((self.root / 'home/alexpi/data/notes.txt').exists())
        self.assertFalse((self.root / 'etc/systemd/journald.conf.d/size.conf').exists())  # config changes are never "safe"
        self.assertIn('LOGGENERATIONS=28', (self.root / 'etc/default/atop').read_text())
        log = (self.root / 'var/log/pi-clean.log').read_text()
        self.assertIn('applied\tapt', log)

    def test_interactive_apply_asks_per_group_and_item(self):
        self.run_main('apply')  # every answer is No
        self.assertEqual(self.docker.mutations(), [])
        categories = [f['category'] for f in self.scanner().scan() if f['safety'] != 'report-only' and f['action']]
        answers = ['e' if c == 'docker-unused-image' else 'n' for c in dict.fromkeys(categories)]
        answers.insert(answers.index('e') + 1, 'y')  # first unused image: yes
        answers.insert(answers.index('e') + 2, 'n')  # second: no
        self.run_main('apply', answers=answers)
        self.assertEqual(self.docker.mutations(), [['docker', 'image', 'rm', 'oldapp-web:signal-prev']])

    def test_stale_findings_are_skipped(self):
        scanner = self.scanner()
        found = {f['id']: f for f in scanner.scan()}
        (self.root / 'var/log/atop/atop_20260901').unlink()
        self.docker.containers.append(container('nginx-again', 'sha256:' + 'f' * 64, 'running', 0))
        out = []
        applier = clean.Applier(scanner, out.append)
        self.assertFalse(applier.apply(found['atop']))
        self.assertFalse(applier.apply(found['image:' + 'f' * 12]))
        self.assertEqual(self.docker.mutations(), [])
        self.assertEqual(sum('no longer applies' in line for line in out), 2)

    def test_deletes_are_confined(self):
        applier = clean.Applier(self.scanner(), lambda line: None)
        self.assertTrue(applier.deletable('/home/alexpi/apps/signal/dist-before-1'))
        self.assertTrue(applier.deletable('/var/log/atop/atop_1'))
        self.assertTrue(applier.deletable('/home/alexpi/apps/tools/__pycache__'))
        for path in ['/home/alexpi/apps/signal', '/home/alexpi/data', '/var/log/atop', '/home/alexpi/apps/signal/../data', '/etc/passwd']:
            self.assertFalse(applier.deletable(path), path)

    def test_parse_size(self):
        self.assertEqual(clean.parse_size('121.0M'), int(121 * 1024 ** 2))
        self.assertEqual(clean.parse_size('4.5GB'), int(4.5 * GB))
        self.assertEqual(clean.parse_size('512kB'), 512000)
        self.assertIsNone(clean.parse_size('lots'))


if __name__ == '__main__':
    unittest.main()
