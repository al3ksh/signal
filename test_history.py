import tempfile
import time
import unittest
from pathlib import Path

from alerts import AlertEngine
from storage import Store


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / 'signal.db'
        self.store = Store(self.path)
        self.now = int(time.time() // 600) * 600

    def tearDown(self):
        self.store.close()
        self.folder.cleanup()

    def add(self, timestamp, cpu=20, temperature=None):
        self.store.add(dict(time=timestamp, cpu=cpu, memory=40, temperature=temperature, rx=1024, tx=512))

    def test_pending_and_saved_values_merge_without_double_counting(self):
        self.add(self.now, 20, 60)
        self.store.flush(self.now)
        self.add(self.now+5, 80, None)
        points = self.store.history(86400, self.now+10)['points']
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]['cpu'], 50)
        self.assertEqual(points[0]['temperature'], 60)
        self.store.flush(self.now+10)
        self.assertEqual(self.store.history(86400, self.now+10)['points'], points)

    def test_persistence_and_gaps(self):
        self.add(self.now-300)
        self.add(self.now)
        self.store.close()
        self.store = Store(self.path)
        points = self.store.history(86400, self.now+5)['points']
        self.assertEqual(len(points), 2)
        self.assertTrue(points[1]['gap'])
        self.assertIsNone(points[0]['temperature'])

    def test_retention_and_week_aggregation(self):
        self.add(self.now-8*86400)
        self.add(self.now-120, 20)
        self.add(self.now-60, 80)
        self.store.flush(self.now)
        result = self.store.history(604800, self.now+5)
        self.assertEqual(result['step'], 600)
        self.assertEqual(len(result['points']), 1)
        self.assertEqual(result['points'][0]['cpu'], 50)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM samples').fetchone()[0], 2)


class AlertTests(HistoryTests):
    def sample(self, offset, state='running', health='healthy', temp=40, errors=None, ignored=False, present=True):
        return dict(timestamp=self.now+offset, errors=errors or [],
                    containers=[dict(name='web',state=state,health=health,ignoreAlerts=ignored)] if present else [],
                    host=dict(cpu=20,temperature=temp,memory=dict(used=30,total=100),disk=dict(used=40,total=100)))

    def test_container_debounce_recovery_and_recurrence(self):
        engine = AlertEngine(self.store)
        engine.evaluate(self.sample(0))
        engine.evaluate(self.sample(5, 'exited'))
        engine.evaluate(self.sample(20, 'exited'))
        self.assertEqual(self.store.alerts(), [])
        engine.evaluate(self.sample(25, 'exited'))
        first = self.store.alerts()[0]
        self.assertTrue(self.store.acknowledge(first['id']))
        engine.evaluate(self.sample(30, 'exited'))
        self.assertEqual(len(self.store.alerts()), 1)
        engine.evaluate(self.sample(35))
        self.assertEqual(self.store.alerts()[0]['resolved'], self.now+35)
        self.assertIsNotNone(self.store.alerts()[0]['acknowledged'])
        engine.evaluate(self.sample(40, 'exited'))
        engine.evaluate(self.sample(60, 'exited'))
        self.assertEqual(len(self.store.alerts()), 2)

    def test_docker_outage_does_not_invent_container_failure(self):
        engine = AlertEngine(self.store)
        engine.evaluate(self.sample(0))
        engine.evaluate(self.sample(5, present=False, errors=['offline']))
        engine.evaluate(self.sample(30, present=False, errors=['offline']))
        self.assertEqual([a['key'] for a in self.store.alerts()], ['docker'])
        engine.evaluate(self.sample(35))
        self.assertIsNotNone(self.store.alerts()[0]['resolved'])

    def test_ignore_jobs_and_old_stopped_containers(self):
        engine = AlertEngine(self.store)
        engine.evaluate(self.sample(0, 'exited'))
        engine.evaluate(self.sample(100, 'exited'))
        self.assertFalse(self.store.alerts())
        engine.evaluate(self.sample(105, ignored=True))
        engine.evaluate(self.sample(110, 'exited', ignored=True))
        engine.evaluate(self.sample(140, 'exited', ignored=True))
        self.assertFalse(self.store.alerts())

    def test_temperature_hysteresis_and_missing_sensor(self):
        engine = AlertEngine(self.store)
        engine.evaluate(self.sample(0, temp=81))
        engine.evaluate(self.sample(21, temp=82))
        engine.evaluate(self.sample(30, temp=77))
        engine.evaluate(self.sample(40, temp=None))
        self.assertIsNone(self.store.alerts()[0]['resolved'])
        engine.evaluate(self.sample(50, temp=74))
        self.assertIsNotNone(self.store.alerts()[0]['resolved'])

    def test_active_alert_and_watched_names_survive_restart(self):
        engine = AlertEngine(self.store)
        engine.evaluate(self.sample(0))
        engine.evaluate(self.sample(5, present=False))
        engine.evaluate(self.sample(30, present=False))
        self.store.close()
        self.store = Store(self.path)
        restarted = AlertEngine(self.store)
        restarted.evaluate(self.sample(50, present=False))
        self.assertEqual(len(self.store.alerts()), 1)
        restarted.evaluate(self.sample(55))
        self.assertIsNotNone(self.store.alerts()[0]['resolved'])

    def test_manual_mute_resolves_and_persists(self):
        engine = AlertEngine(self.store)
        engine.evaluate(self.sample(0))
        engine.evaluate(self.sample(5, 'exited'))
        engine.evaluate(self.sample(26, 'exited'))
        self.assertIsNone(self.store.alerts()[0]['resolved'])

        engine.set_muted('web', True, self.now+27)
        self.assertEqual(self.store.alerts()[0]['resolved'], self.now+27)
        muted = self.sample(30, 'exited')
        engine.evaluate(muted)
        self.assertTrue(muted['containers'][0]['monitoringMuted'])
        self.assertEqual(muted['containers'][0]['monitoringMuteSource'], 'manual')

        restarted = AlertEngine(self.store)
        still_muted = self.sample(40, 'exited')
        restarted.evaluate(still_muted)
        self.assertTrue(still_muted['containers'][0]['monitoringMuted'])
        self.assertEqual(len(self.store.alerts()), 1)

        restarted.set_muted('web', False, self.now+41)
        restarted.evaluate(self.sample(42, 'exited'))
        restarted.evaluate(self.sample(63, 'exited'))
        self.assertEqual(len(self.store.alerts()), 2)


if __name__ == '__main__':
    unittest.main()
