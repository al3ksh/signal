"""Debounced local incidents, keyed by container name across recreations."""
import os
import threading


class AlertEngine:
    def __init__(self, store):
        self.store = store
        self.pending = {}
        self.watched = set(store.metadata('watched', []))
        self.muted = set(store.metadata('mutedContainers', []))
        self.maintenance = store.metadata('maintenance', {})
        self.active = {a['key'] for a in store.alerts() if a['resolved'] is None}
        self.temp_limit = float(os.getenv('SIGNAL_TEMP_LIMIT', '80'))
        self.lock = threading.RLock()

    def active_maintenance(self, now):
        expired = [service for service, until in self.maintenance.items() if until is not None and until <= now]
        if expired:
            for service in expired:
                self.maintenance.pop(service, None)
            self.store.set_metadata('maintenance', self.maintenance)
        return dict(self.maintenance)

    def decorate(self, data):
        with self.lock:
            maintenance = self.active_maintenance(data['timestamp'])
            for container in data.get('containers', []):
                name, service = container['name'], container.get('project', 'standalone')
                label = bool(container.get('ignoreAlerts'))
                source = 'label' if label else ('manual' if name in self.muted else ('maintenance' if service in maintenance else None))
                container['monitoringMuted'] = source is not None
                container['monitoringMuteSource'] = source
                container['maintenanceUntil'] = maintenance.get(service) if service in maintenance else None
            for check in data.get('checks', []):
                service = check.get('service', 'standalone')
                check['maintenanceUntil'] = maintenance.get(service) if service in maintenance else None
            data['maintenance'] = maintenance
            return maintenance

    def set_maintenance(self, service, until, containers, checks, now):
        with self.lock:
            if until is not None and until <= now:
                self.maintenance.pop(service, None)
            else:
                self.maintenance[service] = until
                for container in containers:
                    if container.get('project', 'standalone') == service:
                        key = 'container:' + container['name']
                        self.pending.pop(key, None)
                        if key in self.active:
                            self.store.resolve_alert(key, now)
                            self.active.remove(key)
                for check in checks:
                    if check.get('service', 'standalone') == service:
                        key = 'http:' + check['id']
                        self.pending.pop(key, None)
                        if key in self.active:
                            self.store.resolve_alert(key, now)
                            self.active.remove(key)
            self.store.set_metadata('maintenance', self.maintenance)

    def set_muted(self, name, muted, now):
        with self.lock:
            key = 'container:' + name
            if muted:
                self.muted.add(name)
                self.watched.discard(name)
                self.pending.pop(key, None)
                if key in self.active:
                    self.store.resolve_alert(key, now)
                    self.active.remove(key)
            else:
                self.muted.discard(name)
                self.watched.add(name)
            self.store.set_metadata('mutedContainers', sorted(self.muted))
            self.store.set_metadata('watched', sorted(self.watched))

    def clear_condition(self, key, now):
        with self.lock:
            self.pending.pop(key, None)
            if key in self.active:
                self.store.resolve_alert(key, now)
                self.active.remove(key)

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
        with self.lock:
            now, host = data['timestamp'], data['host']
            maintenance = self.decorate(data)
            docker_ok = not data['errors']
            self.check('docker', not docker_ok, 'Docker is unavailable', 'The Docker daemon cannot be reached.', now, severity='critical')
            if docker_ok:
                before = self.watched.copy()
                current = {c['name']: c for c in data['containers']}
                label_ignored = {c['name'] for c in data['containers'] if c.get('ignoreAlerts')}
                persistent_ignored = label_ignored | self.muted
                maintained = {c['name'] for c in data['containers'] if c.get('project', 'standalone') in maintenance}
                ignored = persistent_ignored | maintained
                self.watched.update(c['name'] for c in data['containers'] if c['state'] == 'running' and c['name'] not in persistent_ignored)
                self.watched -= persistent_ignored
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
            for check in data.get('checks', []):
                maintained = check.get('service', 'standalone') in maintenance
                bad = False if maintained else (None if check.get('ok') is None else not check['ok'])
                self.check('http:'+check['id'], bad,
                           check['name']+' is unreachable', check.get('message') or 'The HTTP check failed.', now,
                           delay=30, severity='critical')
            check_keys = {'http:'+check['id'] for check in data.get('checks', [])}
            for key in list(self.active):
                if key.startswith('http:') and key not in check_keys:
                    self.clear_condition(key, now)
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
