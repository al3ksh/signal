"""Debounced local incidents, keyed by container name across recreations."""
import os


class AlertEngine:
    def __init__(self, store):
        self.store = store
        self.pending = {}
        self.watched = set(store.metadata('watched', []))
        self.active = {a['key'] for a in store.alerts() if a['resolved'] is None}
        self.temp_limit = float(os.getenv('SIGNAL_TEMP_LIMIT', '80'))

    def check(self, key, bad, title, message, now, delay=20, severity='warning'):
        # None means unavailable: never infer recovery from missing telemetry.
        if bad is None:
            self.pending.pop(key, None)
            return
        if bad:
            since = self.pending.setdefault(key, now)
            if key not in self.active and now-since >= delay:
                self.store.open_alert(key, severity, title, message, now)
                self.active.add(key)
        else:
            self.pending.pop(key, None)
            if key in self.active:
                self.store.resolve_alert(key, now)
                self.active.remove(key)

    def evaluate(self, data):
        now, host = data['timestamp'], data['host']
        docker_ok = not data['errors']
        self.check('docker', not docker_ok, 'Docker is unavailable', 'The Docker daemon cannot be reached.', now, severity='critical')
        if docker_ok:
            before = self.watched.copy()
            current = {c['name']: c for c in data['containers']}
            self.watched.update(c['name'] for c in data['containers'] if c['state'] == 'running' and not c.get('ignoreAlerts'))
            ignored = {c['name'] for c in data['containers'] if c.get('ignoreAlerts')}
            self.watched -= ignored
            if before != self.watched:
                self.store.set_metadata('watched', sorted(self.watched))
            for name in self.watched | ignored:
                c = current.get(name)
                bad = name not in ignored and (c is None or c['state'] != 'running' or c['health'] == 'unhealthy')
                state = 'missing' if c is None else ('unhealthy' if c['health'] == 'unhealthy' else c['state'])
                self.check('container:'+name, bad, name+' needs attention', 'Container state: '+state+'.', now, severity='critical')
        else:
            for key in list(self.pending):
                if key.startswith('container:'):
                    self.pending.pop(key)
        temp = host['temperature']
        self.check('temperature', None if temp is None else temp >= self.temp_limit-(5 if 'temperature' in self.active else 0),
                   'Host temperature is high', f'Temperature reached {self.temp_limit:g} °C. Recovery threshold: {self.temp_limit-5:g} °C.', now)
        for key, value, title, limit, recovery in [
            ('cpu', host['cpu'], 'Sustained CPU load', 90, 80),
            ('memory', host['memory']['used']/max(1,host['memory']['total'])*100, 'Memory pressure', 90, 85),
            ('disk', host['disk']['used']/max(1,host['disk']['total'])*100, 'System disk is nearly full', 90, 87),
        ]:
            self.check(key, value >= (recovery if key in self.active else limit), title,
                       f'Usage remained above {limit}% for 60 seconds. Recovery below {recovery}%.', now, delay=60)
