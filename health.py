"""Raspberry Pi health: power, SD card, filesystem, clock, memory pressure, failed units, disk forecast.

Readers are plain functions over root-relative paths (proc_root, sys_root, run_root) so tests can point them at a
fixture tree. Unknown always means None, never a fake OK. Slow commands (vcgencmd, systemctl) are refreshed by
Health on their own cadence in a background thread, so they never hold up the 5 s sample."""
import datetime
import os
from pathlib import Path
import subprocess
import threading
import time

# get_throttled bits: 0-3 are "now", 16-19 the same conditions "since boot". Sticky bits explain random crashes.
THROTTLE = {0: 'under-voltage', 1: 'arm-frequency-capped', 2: 'throttled', 3: 'soft-temperature-limit'}
SLOW_POWER, SLOW_UNITS, FORECAST_EVERY = 30, 60, 600


def text(path):
    try:
        return Path(path).read_text().strip()
    except (OSError, UnicodeError):
        return None


def number(path):
    value = text(path)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def command(argv, timeout=2):
    """stdout of a short command, or None if it is missing, fails or times out."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
        return done.stdout.strip() if done.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def decode_throttle(value):
    if value is None:
        return None
    return {'now': [name for bit, name in THROTTLE.items() if value >> bit & 1],
            'sinceBoot': [name for bit, name in THROTTLE.items() if value >> (bit + 16) & 1], 'raw': hex(value)}


def throttle_value(sys_root, run=command):
    """Prefer the firmware sysfs file (no fork); fall back to vcgencmd, which prints throttled=0x50005."""
    raw = text(Path(sys_root) / 'devices/platform/soc/soc:firmware/get_throttled')
    if raw is None:
        output = run(['vcgencmd', 'get_throttled'])
        raw = output.partition('=')[2] if output and '=' in output else None
    try:
        return int(raw, 16) if raw else None
    except ValueError:
        return None


def core_volts(run=command):
    output = run(['vcgencmd', 'measure_volts', 'core'])  # volt=0.8500V
    try:
        return float(output.partition('=')[2].rstrip('V')) if output else None
    except ValueError:
        return None


def mount_for(proc_root, path='/'):
    """The /proc/mounts entry that holds path (longest mount-point prefix)."""
    best = None
    for line in (text(Path(proc_root) / 'mounts') or '').splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        point = fields[1].replace('\\040', ' ')
        if (path == point or path.startswith(point.rstrip('/') + '/')) and (best is None or len(point) > len(best['point'])):
            best = {'device': fields[0], 'point': point, 'type': fields[2], 'readOnly': 'ro' in fields[3].split(',')}
    return best


def ext4_stats(sys_root, device):
    """Error counters and lifetime writes from /sys/fs/ext4/<dev>/; /dev/root resolves to the only ext4 entry."""
    base = Path(sys_root) / 'fs/ext4'
    name = os.path.basename(device or '')
    if not (base / name).is_dir():
        entries = [p.name for p in base.iterdir() if p.is_dir()] if base.is_dir() else []
        name = entries[0] if len(entries) == 1 else None
    if not name:
        return None
    folder = base / name
    stats = {'device': name, 'errors': number(folder / 'errors_count'), 'firstErrorTime': number(folder / 'first_error_time'),
             'lastErrorTime': number(folder / 'last_error_time'), 'lifetimeWriteKbytes': number(folder / 'lifetime_write_kbytes')}
    return stats if any(stats[k] is not None for k in ('errors', 'lifetimeWriteKbytes')) else None


def inodes(path):
    try:
        info = os.statvfs(path)
    except OSError:
        return None
    return {'total': info.f_files, 'free': info.f_ffree} if info.f_files else None


def clock_synced(run_root):
    """systemd-timesyncd touches this file once NTP is synced. Without timesyncd we cannot tell: None."""
    folder = Path(run_root) / 'systemd/timesync'
    return (folder / 'synchronized').exists() if folder.is_dir() else None


def zram(sys_root):
    total = None
    for stat in sorted((Path(sys_root) / 'block').glob('zram*/mm_stat')):
        fields = (text(stat) or '').split()
        if len(fields) >= 3 and all(f.isdigit() for f in fields[:3]):
            total = total or {'original': 0, 'compressed': 0, 'used': 0}
            total['original'] += int(fields[0])
            total['compressed'] += int(fields[1])
            total['used'] += int(fields[2])
    return total


def major_faults(proc_root):
    for line in (text(Path(proc_root) / 'vmstat') or '').splitlines():
        if line.startswith('pgmajfault '):
            return int(line.split()[1])
    return None


def pressure(proc_root):
    """avg60 of 'some' pressure when the kernel has PSI; None (omitted) otherwise, never zeros."""
    folder = Path(proc_root) / 'pressure'
    result = {}
    for name in ('cpu', 'memory', 'io'):
        for line in (text(folder / name) or '').splitlines():
            if line.startswith('some '):
                values = dict(part.split('=', 1) for part in line.split()[1:] if '=' in part)
                try:
                    result[name] = float(values['avg60'])
                except (KeyError, ValueError):
                    pass
    return result or None


def failed_units(run=command):
    output = run(['systemctl', 'list-units', '--state=failed', '--plain', '--no-legend', '--no-pager'])
    return None if output is None else [line.split()[0] for line in output.splitlines() if line.strip()]


def disk_forecast(points, now, horizon=60):
    """Days until the disk is full from a least-squares line over (time, used %) points; None unless filling up."""
    points = [(t, d) for t, d in points if d is not None and now - t <= 7 * 86400]
    if len(points) < 2 or points[-1][0] - points[0][0] < 12 * 3600:
        return None
    n = len(points)
    mean_t, mean_d = sum(t for t, _ in points) / n, sum(d for _, d in points) / n
    spread = sum((t - mean_t) ** 2 for t, _ in points)
    if not spread:
        return None
    slope = sum((t - mean_t) * (d - mean_d) for t, d in points) / spread * 86400  # % per day
    if slope <= 1e-6:
        return None
    days = (100 - points[-1][1]) / slope
    return {'days': round(days, 1), 'slopePerDay': round(slope, 3)} if 0 <= days < horizon else None


def writes_per_day(snapshots, current_kbytes, now):
    """Average bytes written per day since the oldest daily snapshot in the last 7 days."""
    if current_kbytes is None:
        return None
    recent = [(ts, kb) for ts, kb in snapshots.values() if now - ts <= 7.5 * 86400 and kb <= current_kbytes]
    if not recent:
        return None
    ts, kb = min(recent)
    days = (now - ts) / 86400
    return round((current_kbytes - kb) * 1024 / days) if days >= 0.5 else None


class Health:
    def __init__(self, store=None, proc_root='/proc', sys_root='/sys', run_root='/run', disk_path='/', run=command):
        self.store, self.proc, self.sys, self.run_root, self.disk_path, self.run = store, Path(proc_root), Path(sys_root), Path(run_root), disk_path, run
        self.lock = threading.Lock()
        self.power, self.units = None, None
        self.slow_at = {'power': 0, 'units': 0}
        self.faults = None  # (monotonic time, pgmajfault)
        self.forecast, self.forecast_at = None, 0
        self.thread = None

    def refresh_slow(self, now=None, force=False):
        """vcgencmd every 30 s and systemctl every 60 s; called from the background thread (or tests)."""
        now = time.time() if now is None else now
        if force or now - self.slow_at['power'] >= SLOW_POWER:
            value = throttle_value(self.sys, self.run)
            power = decode_throttle(value)
            if power is not None:
                power['coreVolts'] = core_volts(self.run)
            with self.lock:
                self.power, self.slow_at['power'] = power, now
        if force or now - self.slow_at['units'] >= SLOW_UNITS:
            units = failed_units(self.run)
            with self.lock:
                self.units, self.slow_at['units'] = units, now

    def start(self, stop):
        def loop():
            while not stop.is_set():
                try:
                    self.refresh_slow()
                except Exception as exc:  # a reader bug must not end health collection
                    print('Health reader error:', type(exc).__name__, exc, flush=True)
                stop.wait(5)
        self.thread = threading.Thread(target=loop, daemon=True, name='health')
        self.thread.start()

    def sd_snapshot(self, stats, now):
        """One lifetime_write_kbytes reading per local day in the metadata table, eight days kept."""
        if not self.store or not stats or stats.get('lifetimeWriteKbytes') is None:
            return None
        snapshots = self.store.metadata('sdWrites', {})
        day = datetime.date.fromtimestamp(now).isoformat()
        if day not in snapshots:
            snapshots[day] = [now, stats['lifetimeWriteKbytes']]
            snapshots = dict(sorted(snapshots.items())[-8:])
            self.store.set_metadata('sdWrites', snapshots)
        return writes_per_day(snapshots, stats['lifetimeWriteKbytes'], now)

    def sample(self, now=None):
        now = time.time() if now is None else now
        mount = mount_for(self.proc, self.disk_path)
        sd = ext4_stats(self.sys, mount['device'] if mount else None)
        if sd:
            sd['writesPerDay'] = self.sd_snapshot(sd, now)
        meminfo = {}
        for line in (text(self.proc / 'meminfo') or '').splitlines():
            key, _, rest = line.partition(':')
            if rest.split() and rest.split()[0].isdigit():
                meminfo[key] = int(rest.split()[0]) * 1024
        faults, mono, rate = major_faults(self.proc), time.monotonic(), None
        if faults is not None and self.faults and mono > self.faults[0]:
            rate = round(max(0, faults - self.faults[1]) / (mono - self.faults[0]), 2)
        self.faults = (mono, faults) if faults is not None else None
        memory = {'zram': zram(self.sys), 'swapTotal': meminfo.get('SwapTotal'),
                  'swapUsed': meminfo['SwapTotal'] - meminfo['SwapFree'] if 'SwapTotal' in meminfo and 'SwapFree' in meminfo else None,
                  'majorFaultsPerSec': rate}
        psi = pressure(self.proc)
        if psi is not None:
            memory['pressure'] = psi
        if self.store and now - self.forecast_at >= FORECAST_EVERY:
            points = [(p['time'], p.get('disk')) for p in self.store.history(7 * 86400, now)['points']]
            self.forecast, self.forecast_at = disk_forecast(points, now), now
        with self.lock:
            power, units = self.power, self.units
        return {'power': power, 'sd': sd, 'rootReadOnly': mount['readOnly'] if mount else None, 'rootDevice': mount['device'] if mount else None,
                'inodes': inodes(self.disk_path), 'clockSynced': clock_synced(self.run_root), 'memory': memory,
                'failedUnits': units, 'diskForecast': self.forecast, 'checkedAt': now}
