"""health.py readers against a fixture tree, the disk forecast, and Pi health incidents."""
import tempfile
import time
import unittest
from pathlib import Path

import health
from alerts import AlertEngine
from storage import Store

MOUNTS_RW = 'proc /proc proc rw,relatime 0 0\n/dev/mmcblk0p2 / ext4 rw,noatime 0 0\n/dev/mmcblk0p1 /boot/firmware vfat rw,relatime 0 0\n'


class Tree:
    def __init__(self, root):
        self.root = Path(root)
        self.proc, self.sys, self.run = self.root / 'proc', self.root / 'sys', self.root / 'run'

    def write(self, path, value):
        path = self.root / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        return path

    def pi(self, mounts=MOUNTS_RW):
        self.write('proc/mounts', mounts)
        self.write('proc/meminfo', 'MemTotal: 3884000 kB\nMemAvailable: 2000000 kB\nSwapTotal: 1048576 kB\nSwapFree: 948576 kB\n')
        self.write('proc/vmstat', 'pgfault 100\npgmajfault 1200\n')
        for name, value in {'errors_count': '0', 'first_error_time': '0', 'last_error_time': '0', 'lifetime_write_kbytes': '508000000'}.items():
            self.write(f'sys/fs/ext4/mmcblk0p2/{name}', value + '\n')
        self.write('sys/fs/ext4/features/lazy_itable_init', 'supported\n')  # real kernels list this beside the devices
        self.write('sys/block/zram0/mm_stat', '327680000 102400000 110000000 0 120000000 10 0 0 0\n')
        (self.run / 'systemd/timesync').mkdir(parents=True)
        self.write('run/systemd/timesync/synchronized', '')
        return self


class FakeRun:
    def __init__(self, outputs):
        self.outputs, self.calls = outputs, []

    def __call__(self, argv, timeout=2):
        self.calls.append(argv)
        return self.outputs.get(' '.join(argv))


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tree = Tree(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_throttle_decoding(self):
        self.assertEqual(health.decode_throttle(0x0), {'now': [], 'sinceBoot': [], 'raw': '0x0'})
        self.assertEqual(health.decode_throttle(0x50000), {'now': [], 'sinceBoot': ['under-voltage', 'throttled'], 'raw': '0x50000'})
        self.assertEqual(health.decode_throttle(0x50005), {'now': ['under-voltage', 'throttled'], 'sinceBoot': ['under-voltage', 'throttled'], 'raw': '0x50005'})
        self.assertEqual(health.decode_throttle(0x80008), {'now': ['soft-temperature-limit'], 'sinceBoot': ['soft-temperature-limit'], 'raw': '0x80008'})
        self.assertIsNone(health.decode_throttle(None))

    def test_throttle_prefers_sysfs_then_vcgencmd(self):
        run = FakeRun({'vcgencmd get_throttled': 'throttled=0x50005'})
        self.assertEqual(health.throttle_value(self.tree.sys, run), 0x50005)
        self.tree.write('sys/devices/platform/soc/soc:firmware/get_throttled', '50000\n')
        self.assertEqual(health.throttle_value(self.tree.sys, run), 0x50000)
        self.assertEqual(len(run.calls), 1)
        self.assertIsNone(health.throttle_value(self.tree.root / 'none', FakeRun({})))
        self.assertEqual(health.core_volts(FakeRun({'vcgencmd measure_volts core': 'volt=0.8500V'})), 0.85)

    def test_root_mount_ext4_and_read_only(self):
        self.tree.pi()
        mount = health.mount_for(self.tree.proc, '/')
        self.assertEqual((mount['device'], mount['readOnly']), ('/dev/mmcblk0p2', False))
        self.assertEqual(health.mount_for(self.tree.proc, '/boot/firmware/config.txt')['device'], '/dev/mmcblk0p1')
        self.tree.write('proc/mounts', '/dev/mmcblk0p2 / ext4 ro,noatime 0 0\n')
        self.assertTrue(health.mount_for(self.tree.proc, '/')['readOnly'])
        # systemd's ProtectSystem=strict makes / look ro in our namespace; init's view wins
        self.tree.write('proc/1/mounts', '/dev/mmcblk0p2 / ext4 rw,noatime 0 0\n')
        self.assertFalse(health.mount_for(self.tree.proc, '/')['readOnly'])
        stats = health.ext4_stats(self.tree.sys, '/dev/root')  # older Pi OS reports /dev/root
        self.assertEqual((stats['device'], stats['errors'], stats['lifetimeWriteKbytes']), ('mmcblk0p2', 0, 508000000))
        self.assertIsNone(health.ext4_stats(self.tree.root / 'nothing', '/dev/sda1'))

    def test_memory_readers(self):
        self.tree.pi()
        self.assertEqual(health.zram(self.tree.sys), {'original': 327680000, 'compressed': 102400000, 'used': 110000000})
        self.assertEqual(health.major_faults(self.tree.proc), 1200)
        self.assertIsNone(health.pressure(self.tree.proc))  # kernel without psi=1: omitted, not zeros
        self.tree.write('proc/pressure/memory', 'some avg10=1.00 avg60=2.50 avg300=0.10 total=1\nfull avg10=0 avg60=0 avg300=0 total=0\n')
        self.assertEqual(health.pressure(self.tree.proc), {'memory': 2.5})

    def test_clock_and_units(self):
        self.assertIsNone(health.clock_synced(self.tree.run))  # no timesyncd: unknown
        self.tree.pi()
        self.assertTrue(health.clock_synced(self.tree.run))
        (self.tree.run / 'systemd/timesync/synchronized').unlink()
        self.assertFalse(health.clock_synced(self.tree.run))
        run = FakeRun({'systemctl list-units --state=failed --plain --no-legend --no-pager': 'atop.service loaded failed failed Atop\nfoo.timer loaded failed failed Foo'})
        self.assertEqual(health.failed_units(run), ['atop.service', 'foo.timer'])
        self.assertEqual(health.failed_units(FakeRun({'systemctl list-units --state=failed --plain --no-legend --no-pager': ''})), [])
        self.assertIsNone(health.failed_units(FakeRun({})))

    def test_disk_forecast(self):
        now = 1_800_000_000
        hours = range(0, 7 * 24, 2)
        flat = [(now - h * 3600, 52.0) for h in reversed(hours)]
        growing = [(now - h * 3600, 80 - h / 24 * 1.0) for h in reversed(hours)]  # +1 %/day, 80 % now
        shrinking = [(now - h * 3600, 52 + h / 24) for h in reversed(hours)]
        self.assertIsNone(health.disk_forecast(flat, now))
        self.assertEqual(health.disk_forecast(growing, now), {'days': 20.0, 'slopePerDay': 1.0})
        self.assertIsNone(health.disk_forecast(shrinking, now))
        slow = [(now - h * 3600, 52 - h / 24 * 0.1) for h in reversed(hours)]  # ~480 days: beyond the horizon
        self.assertIsNone(health.disk_forecast(slow, now))
        self.assertIsNone(health.disk_forecast(growing[-3:], now))  # less than 12 h of data

    def test_writes_per_day(self):
        now = 1_800_000_000
        snapshots = {'a': [now - 4 * 86400, 1000], 'b': [now - 86400, 3000], 'old': [now - 20 * 86400, 10]}
        self.assertEqual(health.writes_per_day(snapshots, 5000, now), 1024 * 1000)
        self.assertIsNone(health.writes_per_day({'a': [now - 3600, 10]}, 20, now))


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tree = Tree(self.tmp.name).pi()
        self.store = Store(Path(self.tmp.name) / 'signal.db')

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def collector(self, run):
        return health.Health(self.store, self.tree.proc, self.tree.sys, self.tree.run, disk_path=self.tmp.name, run=run)

    def test_sample_shape_and_slow_cadence(self):
        run = FakeRun({'vcgencmd get_throttled': 'throttled=0x50000', 'vcgencmd measure_volts core': 'volt=0.8600V',
                       'systemctl list-units --state=failed --plain --no-legend --no-pager': ''})
        item = self.collector(run)
        now = time.time()
        item.refresh_slow(now)
        item.refresh_slow(now + 10)
        self.assertEqual(sum(c[:2] == ['vcgencmd', 'get_throttled'] for c in run.calls), 1)
        item.refresh_slow(now + 31)
        self.assertEqual(sum(c[:2] == ['vcgencmd', 'get_throttled'] for c in run.calls), 2)
        data = item.sample(now)
        self.assertEqual(data['power'], {'now': [], 'sinceBoot': ['under-voltage', 'throttled'], 'raw': '0x50000', 'coreVolts': 0.86})
        self.assertEqual((data['sd']['errors'], data['rootReadOnly'], data['clockSynced'], data['failedUnits']), (0, False, True, []))
        self.assertEqual(data['memory']['swapUsed'], 100000 * 1024)
        self.assertNotIn('pressure', data['memory'])
        self.assertIsNone(data['memory']['majorFaultsPerSec'])  # needs two samples
        self.tree.write('proc/vmstat', 'pgmajfault 1300\n')
        self.assertIsNotNone(item.sample(now + 5)['memory']['majorFaultsPerSec'])
        self.assertEqual(self.store.metadata('sdWrites')[time.strftime('%Y-%m-%d', time.localtime(now))][1], 508000000)
        self.assertIsNotNone(data['inodes'])

    def test_unknown_stays_unknown(self):
        empty = health.Health(None, Path(self.tmp.name) / 'p', Path(self.tmp.name) / 's', Path(self.tmp.name) / 'r', disk_path='/nonexistent', run=FakeRun({}))
        empty.refresh_slow(force=True)
        data = empty.sample()
        for key in ('power', 'sd', 'rootReadOnly', 'inodes', 'clockSynced', 'failedUnits', 'diskForecast'):
            self.assertIsNone(data[key], key)

    def test_disk_history_feeds_the_forecast(self):
        now = int(time.time() // 600) * 600
        for i in range(7 * 24):
            self.store.add(dict(time=now - (7 * 24 - i) * 3600, cpu=1, memory=1, temperature=None, rx=0, tx=0, disk=60 + i / 24 * 2))
        self.store.flush(now)
        points = self.store.history(7 * 86400, now)['points']
        self.assertAlmostEqual(points[-1]['disk'], 60 + 167 / 24 * 2, places=3)
        forecast = self.collector(FakeRun({})).sample(now)['diskForecast']
        self.assertAlmostEqual(forecast['slopePerDay'], 2.0, places=2)
        self.assertLess(forecast['days'], 14)


class HealthAlertTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'signal.db')
        self.engine = AlertEngine(self.store)
        self.now = time.time()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def active(self):
        return {a['key']: a for a in self.store.alerts() if a['resolved'] is None}

    def healthy(self, **changes):
        data = {'power': {'now': [], 'sinceBoot': [], 'raw': '0x0'}, 'sd': {'device': 'mmcblk0p2', 'errors': 0, 'lastErrorTime': 0},
                'rootReadOnly': False, 'clockSynced': True, 'failedUnits': [], 'diskForecast': None}
        data.update(changes)
        return data

    def test_power_alerts(self):
        self.engine.evaluate_health(self.healthy(power={'now': ['under-voltage', 'throttled'], 'sinceBoot': ['under-voltage', 'throttled'], 'raw': '0x50005'}), self.now)
        self.assertEqual(self.active()['power:undervoltage']['severity'], 'critical')
        self.assertIn('PSU or cable may be too weak', self.active()['power:history']['message'])
        self.engine.evaluate_health(self.healthy(power={'now': [], 'sinceBoot': ['under-voltage', 'throttled'], 'raw': '0x50000'}), self.now + 30)
        self.assertEqual(set(self.active()), {'power:history'})  # stays open until reboot clears the sticky bits
        self.engine.evaluate_health(self.healthy(), self.now + 60)
        self.assertEqual(self.active(), {})

    def test_sd_errors_open_on_increase(self):
        self.engine.evaluate_health(self.healthy(sd={'device': 'mmcblk0p2', 'errors': 2, 'lastErrorTime': self.now - 30 * 86400}), self.now)
        self.assertEqual(self.active(), {})  # old errors from before SIGNAL watched
        self.engine.evaluate_health(self.healthy(sd={'device': 'mmcblk0p2', 'errors': 3, 'lastErrorTime': self.now}), self.now + 5)
        self.assertEqual(self.active()['sd:errors']['severity'], 'critical')
        self.engine.evaluate_health(self.healthy(sd={'device': 'mmcblk0p2', 'errors': 3, 'lastErrorTime': self.now}), self.now + 8 * 86400)
        self.assertEqual(self.active(), {})

    def test_read_only_units_forecast_and_unknowns(self):
        self.engine.evaluate_health(self.healthy(rootReadOnly=True, failedUnits=['atop.service'], diskForecast={'days': 9.5, 'slopePerDay': 3}), self.now)
        active = self.active()
        self.assertEqual(active['fs:readonly']['severity'], 'critical')
        self.assertIn('atop.service', active['units:failed']['message'])
        self.assertEqual(active['disk:forecast']['severity'], 'warning')
        self.engine.evaluate_health(self.healthy(rootReadOnly=None, failedUnits=None, diskForecast={'days': 18, 'slopePerDay': 1}), self.now + 5)
        self.assertEqual(set(self.active()), {'fs:readonly', 'units:failed', 'disk:forecast'})  # unknown resolves nothing; 18 d is inside hysteresis
        self.engine.evaluate_health(self.healthy(diskForecast={'days': 30, 'slopePerDay': 1}), self.now + 10)
        self.assertEqual(self.active(), {})

    def test_clock_waits_ten_minutes(self):
        self.engine.evaluate_health(self.healthy(clockSynced=False), self.now)
        self.engine.evaluate_health(self.healthy(clockSynced=False), self.now + 300)
        self.assertNotIn('clock:unsynced', self.active())
        self.engine.evaluate_health(self.healthy(clockSynced=False), self.now + 601)
        self.assertIn('2FA', self.active()['clock:unsynced']['message'].replace('Authenticator codes', '2FA'))


if __name__ == '__main__':
    unittest.main()
