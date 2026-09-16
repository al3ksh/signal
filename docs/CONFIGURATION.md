# Configuration

For Compose, copy `.env.example` to `.env` or let the launcher create it. Do not commit `.env`. Rerun `sh ./signal start` after changes. Values set in the invoking shell override `.env` through Compose.

| Variable | Default | Purpose |
| --- | --- | --- |
| `SIGNAL_HOST` | `127.0.0.1` | HTTP bind address. Use loopback behind a local proxy. |
| `SIGNAL_PORT` | `8091` | HTTP port. |
| `SIGNAL_NODE_NAME` | Hostname | Optional display name; no hardware name is hardcoded. |
| `SIGNAL_INTERVAL` | `5` | Backend sample period, in seconds; minimum 3. Independent of browser refresh. |
| `SIGNAL_TEMP_LIMIT` | `80` | Temperature alert threshold in Celsius; clears 5 degrees below it. |
| `DOCKER_SOCKET` | `/var/run/docker.sock` | Host socket path; launcher discovers its group. |
| `DOCKER_GID` | Discovered | Numeric group allowed to access that socket. |

Native-only/path settings (fixed to container mount paths in the supplied Compose file):

| Variable | Native default | Purpose |
| --- | --- | --- |
| `SIGNAL_DATA_DIR` | `./data` beside `server.py` | Private SQLite database directory. |
| `SIGNAL_AUTH_FILE` | `$SIGNAL_DATA_DIR/access.json` | Password hash and signing secret. |
| `SIGNAL_PROC_ROOT` | `/proc` | Host process and memory counters. |
| `SIGNAL_SYS_ROOT` | `/sys` | Optional thermal and frequency counters. |
| `SIGNAL_DISK_PATH` | `/` | Filesystem whose capacity is reported. |
| `SIGNAL_PUBLIC_MODE` | Off | Explicit unauthenticated telemetry mode when no auth file exists. Not recommended; logs and alert acknowledgement remain disabled. |

The first usable CPU thermal-zone readings supply temperature (the highest plausible zone value is used). Hardware throttling uses `vcgencmd` if installed in a native Raspberry Pi environment. The supplied container has no `vcgencmd`; absence is displayed honestly.

## HTTPS reverse proxy

Forward to `http://127.0.0.1:8091`. Preserve the original `Host` and set `X-Forwarded-Proto: https`. SIGNAL uses this header to set Secure cookies. Keep the origin server reachable only by the proxy. Cookies are HttpOnly and SameSite=Strict; sessions last 30 days. Request bodies and access logs should never record passwords.

Cloudflare Tunnel example (merge this into your own tunnel configuration before its fallback rule):

```yaml
ingress:
  - hostname: status.example.com
    service: http://127.0.0.1:8091
  - service: http_status:404
```

Use your tunnel's DNS route and credentials. Do not copy someone else's tunnel ID or credential file. SIGNAL does not create DNS records, install cloudflared, or change your other routes.

Caddy example:

```caddyfile
status.example.com {
    reverse_proxy 127.0.0.1:8091
}
```

When the proxy runs in Docker, its `127.0.0.1` normally refers to its own namespace. Run it with host networking or configure an explicit route to the host and restrict the bind/firewall accordingly.
