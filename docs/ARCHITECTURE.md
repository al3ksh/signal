# Architecture

React/TypeScript renders a static Vite bundle. `server.py` serves that bundle, handles password sessions and collects Linux/Docker telemetry. The backend requires only Python's standard library. `storage.py` owns SQLite persistence; `alerts.py` owns debouncing, hysteresis and incident transitions. `health.py` reads Raspberry Pi health (power bits, SD card, clock, memory pressure, failed units, disk trend) from root-relative paths and caches its slow commands.

```text
Browser -> HTTPS proxy -> SIGNAL HTTP server
                            |-- bounded GET requests -> local Docker Unix socket
                            |-- read counters -> host /proc, /sys, filesystem
                            `-- SQLite + access file -> persistent data directory
```

Optional app control adds one process with a narrower job and a wider privilege:

```text
Browser -> SIGNAL HTTP server (unprivileged, Docker GET only)
              `-- {op, app, action} JSON line -> /run/signal-runner/control.sock
                                                   |
                                     runner.py (docker group, signal-runner.service)
                                       |-- /etc/signal/apps.json (registry, validated at start)
                                       |-- git / docker compose as argv lists, cwd = app dir
                                       `-- /var/lib/signal-runner (jobs.db, logs/<job>.log)
```

`runner.py` accepts only `list`, `status`, `run`, `jobs`, `job`, `log` and `cancel`. `run` takes an app id that must exist in the registry and one of `start`, `stop`, `restart`, `check`, `update`, `rollback`. Commands are fixed argv lists built from registry values; the runner never uses a shell. Jobs run one at a time per app, and only one build or image pull runs at a time on the host. See [APP-CONTROL.md](APP-CONTROL.md).

The collector runs in one background thread. At most four Docker statistics calls run concurrently, each with a timeout. The HTTP handler serves a locked snapshot without starting a new collection for each visitor. History queries aggregate stored buckets, with pending measurements merged under a storage lock. Successful shutdown stops collection and flushes data.

Docker API support is negotiated from `/version`; stats need API 1.41+ (Docker 20.10+). SIGNAL uses GET requests only. See the [Docker Engine 1.41 API](https://docs.docker.com/reference/api/engine/version/v1.41/).

## HTTP endpoints

| Method | Endpoint | Behavior |
| --- | --- | --- |
| GET | `/healthz` | Public boolean liveness/freshness; no host data. 503 before the first sample or when stale. |
| GET | `/api/session` | Session status and enabled capabilities. |
| POST | `/api/login` | JSON `{ "password": "..." }`; sets session cookie. |
| POST | `/api/logout` | Clears this device's cookie. |
| GET | `/api/status` | Current host, containers, live history, process list, events and alerts. |
| GET | `/api/history?range=1h\|24h\|7d` | Bounded persistent telemetry points, bucket step and retention. |
| GET | `/api/brief` | Aggregated 24-hour coverage, incident, uptime, CPU and temperature summary. |
| GET | `/api/alerts` | Current and recent incidents. |
| POST | `/api/alerts/{id}/ack` | Marks an incident seen; private session and same-origin request required. |
| POST | `/api/containers/{id}/monitoring` | Persistently mutes or unmutes SIGNAL incidents for a container; never mutates Docker. |
| POST | `/api/maintenance` | Starts, changes or ends a service maintenance window. |
| POST | `/api/checks` | Adds, updates or removes a persisted HTTP endpoint check. |
| GET | `/api/containers/{id}/details` | Filtered inspect data; no environment variables or host mount sources. |
| GET | `/api/containers/{id}/logs` | Last 160 lines, bounded response; private sessions only. |
| GET | `/api/apps` | Only with app control configured: registry apps joined with observed containers, deployed commit, commits behind, last job and `control` capabilities. No host paths. |
| GET | `/api/apps/{id}/jobs` | Last 20 runner jobs for one app. |
| GET | `/api/jobs/{id}` | One job: status, phase, previous/new commit, message. |
| GET | `/api/jobs/{id}/log?offset=n` | Incremental job log as JSON `{data, offset, done, status}`; private sessions only. |
| POST | `/api/apps/{id}/{action}` | `start`, `stop`, `restart`, `check`, `update`, `rollback`. Allowed network, same-origin JSON and password step-up required (except `check`). 202 with the queued job. |
| POST | `/api/jobs/{id}/cancel` | Cancels a queued or running job; same gate as actions. |

All telemetry endpoints require authentication by default. Mutations require the private password session and reject cross-origin requests. Container identifiers must be a known 12-character ID from the snapshot. The web server has no Docker start, stop, restart, exec or proxy routes. App lifecycle requests go to the runner as `{app, action}` and exist only when `SIGNAL_CONTROL_SOCKET` is set; all `/api/apps` and `/api/jobs` routes answer 404 otherwise. The API is internal and may evolve; it is not versioned as a public integration contract.
