# Alerts and history

## Saved telemetry

Host CPU, memory, temperature and network throughput are collected every 5 seconds by default. Live charts keep about one hour in memory. SQLite stores sample-weighted minute averages and retains 7 days. The archive uses minute buckets for 1h/24h and ten-minute buckets for 7d. Short spikes can disappear in averages; the live view is better for transient activity.

Writes are buffered for up to 30 seconds to reduce storage churn. A normal shutdown flushes the buffer; a power failure can lose the last buffered interval. SQLite uses WAL. Restarting SIGNAL preserves saved measurements and alerts. Downtime is not filled with invented values; chart paths break between missing buckets. A partially filled bucket represents only observed samples, not the entire time bucket.

History starts at installation. Container logs and per-container CPU/RAM history are not persisted. The signal journal remains a short in-memory list of container state transitions; the alert journal is durable.

## Incident rules

| Condition | Opens after | Recovery |
| --- | --- | --- |
| Watched container stopped, missing, paused, restarting or unhealthy | 20 seconds | Running without an unhealthy healthcheck |
| Docker daemon unreachable | 20 seconds | Docker inventory can be read |
| Host temperature at least 80°C (configurable) | 20 seconds | Below 75°C (threshold minus 5) |
| CPU at least 90% | 60 seconds | Below 80% |
| RAM usage at least 90% | 60 seconds | Below 85% |
| System disk usage at least 90% | 60 seconds | Below 87% |

Detection is sampled, so opening can be delayed by one sample interval. An unavailable temperature does not create an alarm or falsely resolve an existing one. During a Docker outage, existing container incidents remain unchanged; an unreadable inventory does not mean every container disappeared.

A container becomes watched after it has been observed running. Already stopped jobs do not create startup alarms. Identity uses the container name, so recreating a container with the same name can resolve its incident. Watched names persist across restarts. Intentionally removing a watched service without ignoring it first produces a missing-container incident.

Exclude scheduled jobs or intentionally temporary services using a Docker/Compose label:

```yaml
services:
  batch-job:
    image: your-job-image
    labels:
      signal.ignore: "true"
```

Compose one-off jobs are also ignored. For intentional decommissioning, apply the ignore label and allow a sample before removing the service. Label changes require recreating the affected container using its own deployment workflow.

One incident stays open for each condition until recovery. A recurrence creates a new incident. Acknowledgement marks an incident as seen by this shared-password account; it does not suppress the condition or resolve it. Resolved incidents are kept for 7 days; active incidents stay until recovery. The panel displays up to 250 incidents.

## Notification scope

This release records incidents in the panel only. It does not request browser notification permission, send webhooks, or contact a notification service.

A future browser integration could notify while the page is open; delivery with the page closed requires Web Push infrastructure. Discord could receive incident/recovery messages through a private channel webhook. ntfy could send authenticated topic messages to a phone, using either a hosted or self-hosted service. These are possible extensions, not currently implemented settings.

An application on the monitored server cannot report a total power or network outage after it loses connectivity. Use an independent external uptime monitor for that case. Alerts here are operational hints, not guaranteed paging or an incident-management service.
