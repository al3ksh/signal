"""Small, persistent telemetry store. Minute buckets limit disk writes on SBCs."""
import json
from pathlib import Path
import sqlite3
import threading
import time


class Store:
    def __init__(self, path, retention_days=7):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS samples (
              time INTEGER PRIMARY KEY, n INTEGER, cpu REAL, memory REAL,
              temperature REAL, temp_n INTEGER, rx REAL, tx REAL);
            CREATE TABLE IF NOT EXISTS alerts (
              id INTEGER PRIMARY KEY, key TEXT, severity TEXT, title TEXT,
              message TEXT, opened REAL, resolved REAL, acknowledged REAL);
            CREATE UNIQUE INDEX IF NOT EXISTS active_alert ON alerts(key) WHERE resolved IS NULL;
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT);
        ''')
        self.pending = {}
        self.retention = retention_days * 86400
        self.last_flush = time.monotonic()

    def add(self, point):
        with self.lock:
            minute = int(point['time'] // 60) * 60
            row = self.pending.setdefault(minute, [minute, 0, 0., 0., 0., 0, 0., 0.])
            row[1] += 1
            row[2] += point['cpu']
            row[3] += point['memory']
            if point['temperature'] is not None:
                row[4] += point['temperature']
                row[5] += 1
            row[6] += point['rx']
            row[7] += point['tx']
            if time.monotonic() - self.last_flush >= 30:
                self.flush(point['time'])

    def flush(self, now=None):
        with self.lock, self.db:
            self.db.executemany('''INSERT INTO samples VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(time) DO UPDATE SET n=n+excluded.n, cpu=cpu+excluded.cpu,
                memory=memory+excluded.memory, temperature=temperature+excluded.temperature,
                temp_n=temp_n+excluded.temp_n, rx=rx+excluded.rx, tx=tx+excluded.tx''', self.pending.values())
            self.pending.clear()
            cutoff = (now if now is not None else time.time()) - self.retention
            self.db.execute('DELETE FROM samples WHERE time < ?', (cutoff,))
            self.db.execute('DELETE FROM alerts WHERE resolved IS NOT NULL AND resolved < ?', (cutoff,))
            self.last_flush = time.monotonic()

    def history(self, seconds, now=None):
        now = time.time() if now is None else now
        step = 600 if seconds > 86400 else 60
        with self.lock:
            rows = {r['time']: list(r) for r in self.db.execute('SELECT * FROM samples WHERE time >= ? ORDER BY time', (now-seconds,))}
            for minute, row in self.pending.items():
                if minute >= now-seconds:
                    if minute in rows:
                        rows[minute] = [minute] + [a+b for a, b in zip(rows[minute][1:], row[1:])]
                    else:
                        rows[minute] = row[:]
            buckets = {}
            for row in rows.values():
                bucket = row[0] // step * step
                total = buckets.setdefault(bucket, [0] * 7)
                for i, value in enumerate(row[1:]):
                    total[i] += value
            points = []
            previous = None
            for bucket, (n, cpu, memory, temp, tn, rx, tx) in sorted(buckets.items()):
                points.append(dict(time=bucket, cpu=cpu/n, memory=memory/n, temperature=temp/tn if tn else None,
                                   rx=rx/n, tx=tx/n, gap=previous is not None and bucket-previous > step*1.5))
                previous = bucket
            return {'points': points, 'step': step, 'retentionDays': self.retention // 86400}

    def metadata(self, key, default=None):
        with self.lock:
            row = self.db.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set_metadata(self, key, value):
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', (key, json.dumps(value)))

    def alerts(self):
        with self.lock:
            return [dict(r) for r in self.db.execute('SELECT * FROM alerts ORDER BY resolved IS NULL DESC, opened DESC LIMIT 250')]

    def daily_brief(self, now=None):
        now = time.time() if now is None else now
        since = now - 86400
        history = self.history(86400, now)['points']
        with self.lock:
            incidents = [dict(r) for r in self.db.execute(
                'SELECT * FROM alerts WHERE opened >= ? ORDER BY opened', (since,))]
        values = lambda field: [p[field] for p in history if p.get(field) is not None]
        cpu, temperatures = values('cpu'), values('temperature')
        intervals = sorted((max(since, a['opened']), min(now, a['resolved'] or now)) for a in incidents
                           if a['severity'] == 'critical' and (a['key'] == 'docker' or a['key'].startswith(('container:', 'http:'))))
        merged = []
        for start, end in intervals:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        downtime = sum(end-start for start, end in merged)
        return {
            'since': since, 'until': now, 'samples': len(history),
            'coverage': min(100, len(history) / 1440 * 100),
            'observedUptime': max(0, (86400-downtime) / 86400 * 100),
            'incidents': len(incidents), 'recoveries': sum(a['resolved'] is not None for a in incidents),
            'averageCpu': sum(cpu)/len(cpu) if cpu else None,
            'peakTemperature': max(temperatures) if temperatures else None,
        }

    def open_alert(self, key, severity, title, message, now):
        with self.lock, self.db:
            self.db.execute('INSERT OR IGNORE INTO alerts(key,severity,title,message,opened) VALUES (?,?,?,?,?)', (key,severity,title,message,now))

    def resolve_alert(self, key, now):
        with self.lock, self.db:
            self.db.execute('UPDATE alerts SET resolved=? WHERE key=? AND resolved IS NULL', (now,key))

    def acknowledge(self, alert_id):
        with self.lock, self.db:
            result = self.db.execute('UPDATE alerts SET acknowledged=COALESCE(acknowledged,?) WHERE id=?', (time.time(),alert_id))
            return result.rowcount > 0

    def close(self):
        with self.lock:
            self.flush()
            self.db.close()
