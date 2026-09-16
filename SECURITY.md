# Security

SIGNAL is a private single-user dashboard. Keep the backend on loopback behind a trusted HTTPS proxy, or use an SSH tunnel. Do not expose the Python HTTP server directly to the internet. It is a small standard-library server, not a hardened public edge server.

The access file contains a salted PBKDF2 password hash and a random HMAC session-signing key, written with mode 600. The generated password is shown once. Sessions last 30 days. Rotating the access file revokes every session. Five login attempts per minute per address are permitted; restart clears this in-memory limiter. Proxy-derived client IPs are trusted only from loopback. No multi-user accounts or per-user audit trail are provided.

The frontend and telemetry API remain behind the same origin. Authentication, request-origin checks for writes, HttpOnly cookies, SameSite=Strict, a restrictive content security policy and path containment checks are included. Log output can contain secrets emitted by your applications; access to this dashboard is access to that log output. Container environment variables and host mount source paths are not returned by the inspector.

## Docker and host access

**Access to the Docker socket is powerful.** A read-only socket bind mount does not make the Docker API read-only. SIGNAL itself only issues GET requests, but a compromised process with socket access could control Docker and escalate to host access. Run it only on a host whose monitoring you control. A tightly scoped Docker socket proxy can be introduced separately if your threat model requires it; it is not included in the one-command setup.

The Compose service runs non-root, drops capabilities, disallows new privileges and uses a read-only application filesystem. It binds host `/proc`, `/sys` and `/` read-only to report host measurements. The root mount is used for filesystem capacity, but also exposes files readable by the container's UID/GIDs. These mounts are an intentional monitoring trust boundary, not isolation from the host. Do not use this configuration on an untrusted multi-tenant host.

History contains host metrics and container names. The data directory and backups include credentials/signing material. Never commit or publicly upload `.env`, `access.json`, `ACCESS.txt`, databases, private logs, screenshots of private infrastructure or data archives. The repository and image use ignore rules to exclude them. Static assets contain no password or deployment-specific secret.

## Reporting

Do not publish secrets or exploit details in a public issue. Contact the repository owner privately or use their private security-reporting channel once configured. This source tree does not claim an unconfigured security contact or disclosure program.
