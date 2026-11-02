# Alerts and history

## Saved telemetry

Host CPU, memory, temperature and network throughput are collected every 5 seconds by default. Live charts keep about one hour in memory. SQLite stores sample-weighted minute averages and retains 7 days. The archive uses minute buckets for 1h/24h and ten-minute buckets for 7d. Short spikes can disappear in averages; the live view is better for transient activity.

Writes are buffered for up to 30 seconds to reduce storage churn. A normal shutdown flushes the buffer; a power failure can lose the last buffered interval. SQLite uses WAL. Restarting SIGNAL preserves saved measurements and alerts. Downtime is not filled with invented values; chart paths break between missing buckets. A partially filled bucket represents only observed samples, not the entire time bucket.

History starts at installation. Container logs and per-container CPU/RAM history are not persisted. The signal journal remains a short in-memory list of container state transitions; the alert journal is durable. The history screen places incidents on the telemetry timeline. Selecting a marker draws the incident time through each chart so host conditions can be inspected around the failure.

## Incident rules

| Condition | Opens after | Recovery |
| --- | --- | --- |
| Watched container stopped, missing, paused, restarting or unhealthy | 20 seconds | Running without an unhealthy healthcheck |
| Docker daemon unreachable | 20 seconds | Docker inventory can be read |
| Host temperature at least 80°C (configurable) | 20 seconds | Below 75°C (threshold minus 5) |
| CPU at least 90% | 60 seconds | Below 80% |
| RAM usage at least 90% | 60 seconds | Below 85% |
| System disk usage at least 90% | 60 seconds | Below 87% |
| Configured HTTP endpoint returns an error or cannot connect | 30 seconds | Successful HTTP 2xx/3xx response |
| App control only: an app's last start/stop/restart/update/rollback job failed (critical) or was rolled back (warning) | Within 30 seconds of the job ending | The next job for that app succeeds; also suppressed by the app's maintenance window |
| Raspberry Pi: under-voltage right now (`get_throttled` bit 0) | Immediately | Bit clears |
| Raspberry Pi: any since-boot power/heat bit (16-19), "the PSU or cable may be too weak" (warning) | Immediately | Reboot clears the sticky bits |
| ext4 error count on the root filesystem increased (critical) | Immediately | 7 days without a new error |
| Root filesystem mounted read-only (critical) | Immediately | Mounted read-write |
| NTP not synchronized (`/run/systemd/timesync/synchronized` missing; warning) | 10 minutes | Synchronized |
| One or more failed systemd units (warning, names in the message) | Within 60 seconds | No failed units |
| System disk full in under 14 days at the 7-day trend (warning) | Immediately | Forecast above 21 days or not filling |

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

For services that are switched off temporarily, open the container inspector and choose **Mute alerts**. SIGNAL keeps the container and its real Docker state visible, removes it from the needs-attention count, resolves any open incident for that container, and suppresses new incidents until **Unmute alerts** is selected. This preference is stored in SIGNAL's SQLite database and survives restarts. It does not start, stop, or modify the Docker container. A `signal.ignore: "true"` label takes precedence and cannot be unmuted from the dashboard.

Maintenance works at the Compose-project service level. A window can last one hour, 24 hours, until a custom date, or until manually resumed. It resolves open container and HTTP-check incidents in that service and suppresses new ones while preserving real Docker and HTTP state in the interface. Expired windows resume monitoring automatically. Maintenance preferences survive SIGNAL restarts.

One incident stays open for each condition until recovery. A recurrence creates a new incident. Acknowledgement marks an incident as seen by this shared-password account; it does not suppress the condition or resolve it. Resolved incidents are kept for 7 days; active incidents stay until recovery. The panel displays up to 250 incidents.

## Notification scope

This release records incidents in the panel only. It does not request browser notification permission, send webhooks, or contact a notification service.

A future browser integration could notify while the page is open; delivery with the page closed requires Web Push infrastructure. Discord could receive incident/recovery messages through a private channel webhook. ntfy could send authenticated topic messages to a phone, using either a hosted or self-hosted service. These are possible extensions, not currently implemented settings.

An application on the monitored server cannot report a total power or network outage after it loses connectivity. Use an independent external uptime monitor for that case. Alerts here are operational hints, not guaranteed paging or an incident-management service.

## Raspberry health

The overview's **Is the Raspberry OK?** card (`health.py`) shows one row per check, each with a reason, and "unavailable" whenever a value cannot be read; it never shows an unknown as OK.

- **Power:** every `get_throttled` bit decoded, now (0-3) and since boot (16-19), plus core voltage. Read from the firmware sysfs file when present, otherwise `vcgencmd`, at most every 30 seconds.
- **SD card:** ext4 `errors_count` and `lifetime_write_kbytes` from `/sys/fs/ext4/<device>/`, with a daily snapshot to show writes per day over the last week, and whether `/` is mounted read-only.
- **Clock:** whether systemd-timesyncd reports NTP sync. Authenticator codes depend on it.
- **Memory:** zram original and compressed size, swap use, major page faults per second, and PSI `avg60` only when the kernel provides `/proc/pressure`.
- **Failed units:** `systemctl list-units --state=failed`, every 60 seconds, unprivileged.
- **Disk:** a linear fit over the last 7 days of disk use (now stored with every history point) estimates "full in N days" when the disk is growing and N is under 60; inode use alongside.

`vcgencmd` and `systemctl` run in a background thread on their own cadence, so they never delay the 5-second sample.
