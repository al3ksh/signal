"""Failed and rolled-back app jobs become local incidents."""
import tempfile
import time
import unittest
from pathlib import Path

from alerts import AlertEngine
from storage import Store


def app(status, action='update', app_id='tools', message='Health check failed after 90 s: web is exited'):
    return {'id': app_id, 'name': 'Tools', 'lastDeploy': None if status is None else {'id': 'f' * 32, 'action': action, 'status': status, 'message': message}}


class DeployAlertTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.folder.name) / 'signal.db')
        self.engine = AlertEngine(self.store)
        self.now = time.time()

    def tearDown(self):
        self.store.close()
        self.folder.cleanup()

    def active(self):
        return {a['key']: a for a in self.store.alerts() if a['resolved'] is None}

    def test_failed_update_opens_critical_incident_and_success_resolves_it(self):
        self.engine.evaluate_deploys([app('failed')], self.now)
        alert = self.active()['deploy:tools']
        self.assertEqual((alert['severity'], alert['title']), ('critical', 'Tools: update failed'))
        self.assertIn('web is exited', alert['message'])
        self.engine.evaluate_deploys([app('running')], self.now + 5)
        self.assertIn('deploy:tools', self.active())  # a job in progress changes nothing yet
        self.engine.evaluate_deploys([app('succeeded')], self.now + 10)
        self.assertNotIn('deploy:tools', self.active())

    def test_rollback_is_a_warning(self):
        self.engine.evaluate_deploys([app('rolled_back')], self.now)
        alert = self.active()['deploy:tools']
        self.assertEqual((alert['severity'], alert['title']), ('warning', 'Tools: update rolled back'))

    def test_cancelled_and_missing_jobs_are_not_incidents(self):
        self.engine.evaluate_deploys([app('cancelled'), app(None, app_id='other')], self.now)
        self.assertEqual(self.active(), {})

    def test_maintenance_suppresses_and_removed_apps_resolve(self):
        self.engine.maintenance['tools'] = None
        self.engine.evaluate_deploys([app('failed')], self.now)
        self.assertEqual(self.active(), {})
        self.engine.maintenance.clear()
        self.engine.evaluate_deploys([app('failed')], self.now)
        self.assertIn('deploy:tools', self.active())
        self.engine.evaluate_deploys([], self.now + 1)
        self.assertEqual(self.active(), {})


if __name__ == '__main__':
    unittest.main()
