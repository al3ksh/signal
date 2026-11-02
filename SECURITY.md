# Security

SIGNAL is a private single-user dashboard. Keep the backend on loopback behind a trusted HTTPS proxy, or use an SSH tunnel. Do not expose the Python HTTP server directly to the internet. It is a small standard-library server, not a hardened public edge server.

The access file contains a salted PBKDF2 password hash and a random HMAC session-signing key, written with mode 600. The generated password is shown once. Sessions last 30 days. Rotating the access file revokes every session. Five login attempts per minute per address are permitted; restart clears this in-memory limiter. Proxy-derived client IPs are trusted only from loopback. No multi-user accounts or per-user audit trail are provided.

The frontend and telemetry API remain behind the same origin. Authentication, request-origin checks for writes, HttpOnly cookies, SameSite=Strict, a restrictive content security policy and path containment checks are included. Log output can contain secrets emitted by your applications; access to this dashboard is access to that log output. Container environment variables and host mount source paths are not returned by the inspector.

## Docker and host access

**Access to the Docker socket is powerful.** A read-only socket bind mount does not make the Docker API read-only. SIGNAL itself only issues GET requests, but a compromised process with socket access could control Docker and escalate to host access. Run it only on a host whose monitoring you control. A tightly scoped Docker socket proxy can be introduced separately if your threat model requires it; it is not included in the one-command setup.

The Compose service runs non-root, drops capabilities, disallows new privileges and uses a read-only application filesystem. It binds host `/proc`, `/sys` and `/` read-only to report host measurements. The root mount is used for filesystem capacity, but also exposes files readable by the container's UID/GIDs. These mounts are an intentional monitoring trust boundary, not isolation from the host. Do not use this configuration on an untrusted multi-tenant host.

History contains host metrics and container names. The data directory and backups include credentials/signing material. Never commit or publicly upload `.env`, `access.json`, `ACCESS.txt`, databases, private logs, screenshots of private infrastructure or data archives. The repository and image use ignore rules to exclude them. Static assets contain no password or deployment-specific secret.

## App control (optional)

App control is off unless `SIGNAL_CONTROL_SOCKET` is set. When it is on, mutations go to a separate runner (`runner.py`) whose process is in the `docker` group. **Membership of the docker group is equivalent to root on the host**, so the runner and its socket are the trust boundary. The web server sends only an app id from the registry and an action from a fixed set; the runner validates both again and builds argv lists from the root-owned registry, never a shell string. If the web server already runs as a user with Docker socket access, the separation is a code-level boundary (it still issues only GET requests), not an OS-level one. To make it an OS-level boundary, run the web server as a separate user without the docker group and point `DOCKER_SOCKET` at the runner's read-only proxy socket, which forwards only allow-listed GET reads (see \"Run SIGNAL without Docker access\" in docs/APP-CONTROL.md).

Mutations require all of: a private session, a request that did not come through Cloudflare (any `Cf-Ray`/`CF-Connecting-IP`/`Cf-Visitor`/`Cf-IPCountry` header means public and read-only), a socket peer inside `SIGNAL_CONTROL_ALLOW` (default Tailscale `100.64.0.0/10`, `fd7a:115c:a1e0::/48` and loopback), a same-origin `application/json` request (`Origin` matching `Host`, or `Sec-Fetch-Site: same-origin`), and a password step-up that mints a 10-minute HttpOnly grant bound to the session. `X-Forwarded-For` and `CF-Connecting-IP` are never used to authorize. If you put another reverse proxy that carries public traffic in front of SIGNAL on loopback, remove `127.0.0.1/32` and `::1/128` from `SIGNAL_CONTROL_ALLOW`. Control requests are rate-limited (20 per minute, 5 failed step-ups per minute) and every allowed or refused request is written to the service log; requests from signed-in sessions also appear in the Signal journal. Job logs can contain build output and are shown only to private sessions.

## Reporting

Do not publish secrets or exploit details in a public issue. Contact the repository owner privately or use their private security-reporting channel once configured. This source tree does not claim an unconfigured security contact or disclosure program.
