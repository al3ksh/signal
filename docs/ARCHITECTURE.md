# Architecture

React/TypeScript renders a static Vite bundle. `server.py` serves that bundle, handles password sessions and collects Linux/Docker telemetry. The backend requires only Python's standard library. `storage.py` owns SQLite persistence; `alerts.py` owns debouncing, hysteresis and incident transitions.

```text
Browser -> HTTPS proxy -> SIGNAL HTTP server
                            |-- bounded GET requests -> local Docker Unix socket
                            |-- read counters -> host /proc, /sys, filesystem
                            `-- SQLite + access file -> persistent data directory
```

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
| GET | `/api/alerts` | Current and recent incidents. |
| POST | `/api/alerts/{id}/ack` | Marks an incident seen; private session and same-origin request required. |
| GET | `/api/containers/{id}/details` | Filtered inspect data; no environment variables or host mount sources. |
| GET | `/api/containers/{id}/logs` | Last 160 lines, bounded response; private sessions only. |

All telemetry endpoints require authentication by default. Container identifiers must be a known 12-character ID from the snapshot. There are no start, stop, restart, exec or arbitrary Docker proxy routes. The API is internal and may evolve; it is not versioned as a public integration contract.
