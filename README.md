# SIGNAL

A private observatory for a Linux server and its Docker containers. Live host telemetry, an interactive container map, persistent history and local incident tracking in a graphite interface.

## Start in one command

Requirements: a **Linux host**, **Docker Engine 20.10+**, and **Docker Compose v2.20+**. The same source builds on **x86_64 and ARM64**, including Raspberry Pi OS 64-bit. Run the commands on the host you want to monitor.

Clone the repository and start SIGNAL:

```sh
git clone https://github.com/al3ksh/signal.git
cd signal
sh ./signal start
```

This builds the image, discovers the Docker socket group, creates a private access password and starts the dashboard. **Save the password printed on the first run.** Later starts preserve it. No Node or Python installation is needed on the host.

The dashboard listens at **http://127.0.0.1:8091** by default. From another computer:

```sh
ssh -N -L 8091:127.0.0.1:8091 user@your-server
```

Open `http://localhost:8091` on that computer. For a domain, point an HTTPS reverse proxy or Cloudflare Tunnel at the same loopback address. For direct trusted LAN access, set `SIGNAL_HOST=0.0.0.0` in `.env`, configure your firewall, and run `sh ./signal start` again. Use HTTPS outside a trusted private network.

## What you get

- Docker state, health, CPU, RAM, ports, metadata and bounded log inspection. Container controls are intentionally read-only.
- Host CPU cores, RAM, root filesystem usage, load, network throughput and top visible processes. Temperature is optional; unsupported hardware shows an unavailable state.
- Live charts plus **1 hour / 24 hours / 7 days** of saved host telemetry. History accumulates from installation; it cannot reconstruct earlier measurements.
- Persistent alerts for stopped, missing or unhealthy watched containers, Docker outages, high temperature, sustained CPU/RAM pressure and disk capacity.
- Service cards group Compose projects, aggregate their resource use and combine container state with opt-in HTTP endpoint checks.
- Maintenance windows suppress expected service incidents for an hour, a day, a custom end time or until manually resumed.
- A daily system brief, incident markers over saved telemetry and a global `Ctrl+K` command palette shorten routine diagnosis.
- An interactive orbit/matrix, filtering, pinning, chart inspection, log filtering/following, custom themes and reduced motion.
- One password, a 30-day session per device, no external telemetry service, and self-hosted fonts.

## Everyday commands

```sh
sh ./signal status   # Container and health status
sh ./signal logs     # Follow application logs
sh ./signal doctor   # Check prerequisites and Compose configuration
sh ./signal stop     # Stop; keep credentials and history
sh ./signal backup   # Briefly stop, archive private data, restart
sh ./signal update   # Rebuild from the source currently on disk
```

`update` does not fetch Git changes. Pull or copy reviewed source first. Data lives in the `signal-data` Docker volume, scoped to the Compose project. Do not use `docker compose down -v` unless you intend to erase the password and history.

## Documentation

- [Installation, native systemd, updates and recovery](docs/INSTALLATION.md)
- [Configuration and reverse proxies](docs/CONFIGURATION.md)
- [History and alert behavior](docs/ALERTS-AND-HISTORY.md)
- [Architecture and API](docs/ARCHITECTURE.md)
- [Security and deployment boundaries](SECURITY.md)
- [Development and contribution checks](CONTRIBUTING.md)
- [Changes](CHANGELOG.md)

This is a **single-host Linux monitor**. It does not monitor Windows/macOS hosts through Docker Desktop, operate remote Docker contexts, orchestrate a cluster, or send notifications when the entire host is offline. 32-bit ARM is not supported by the supplied image. Container telemetry can work without hardware sensors; process visibility depends on host permissions.

## Repository status

Version 2.2.0. CI builds the frontend and container and runs backend tests. Secrets, private deployment files, runtime databases, backups and generated builds are excluded from source control. A distribution license has not been selected; choose one before publishing this as an open-source project. Third-party packages retain their own licenses.
