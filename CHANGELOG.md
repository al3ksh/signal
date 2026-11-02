# Changelog

## Unreleased

- Added Raspberry Pi health: all `get_throttled` bits decoded (now and since boot) with core voltage, SD card ext4 errors and writes per day, read-only root detection, inodes, NTP sync, zram/swap/major faults (PSI when available), failed systemd units and a 7-day "disk full in N days" forecast. Shown in an overview card and on the temperature card, with incidents for under-voltage, power history, SD errors, read-only root, clock, failed units and the disk forecast. Disk use is now part of the history.

- Added `deploy/deploy.py`, a stdlib deploy script that needs only ssh (works from Windows Git Bash): it ships only what changed, restarts only the affected unit, verifies `/healthz`, the served assets and unit state, rolls back by itself, keeps five `releases/<stamp>/` snapshots, supports `--dry-run`, `--list`, `--rollback` and `--prune-legacy`, and refuses to start if sudo would prompt. `update-signal.sh` is now a wrapper around it.

- Added an optional app registry and a separate runner (`runner.py`) that reports each Compose app's deployed commit and how many commits it is behind its repository branch. A new Apps view appears only when `SIGNAL_CONTROL_SOCKET` is set and the runner is reachable.
- Added start, stop and restart for registered apps through the runner. Control is refused through the public tunnel, needs a same-origin request and a password step-up (10-minute grant), is rate-limited and is recorded in the journal. Ships a hardened `deploy/signal-runner.service`.
- Added one-click update from the repository (fast-forward only, refused on a dirty or diverged checkout) with live job logs, a phase indicator, container and HTTP health checks and automatic rollback to the exact previous commit, reusing the previous images when they were saved. Added manual rollback, job history, job cancellation and command-palette entries for updating, restarting and opening job logs.
- Faster updates: `pull` mode deploys CI-built images pinned to the deployed commit (`SIGNAL_COMMIT`), build mode rebuilds only services whose build context changed, a reusable arm64 GitHub Actions template (`docs/templates/build-arm64.yml`), and `deploy/update-signal.sh` for updating SIGNAL itself. See `docs/APP-CONTROL.md`.
- Added a Fluent-style motion layer: pointer-tracked reveal light and edge glow, a sliding navigation indicator, acrylic dialogs, staggered card entrances, pressed states, a job progress rail with phase shimmer, and a decorative ambient signal field behind the interface. All of it respects reduced motion, the quiet motion setting, paused observation and hidden tabs.
- Deploys cut off by a runner restart are rolled back automatically on the next start, while the checkout is still where the interrupted job left it.
- Pull-mode updates wait for CI: they stop with a clear message until the images pinned to the new commit are published.
- Long job logs keep their beginning and the latest output instead of being cut at 2 MiB.
- Failed app jobs open a critical incident and rolled-back ones a warning in Alerts & events.
- Optional read-only Docker socket served by the runner, so SIGNAL's web server can run as its own user without Docker access.
- Fonts are emitted as files, fixing a Content-Security-Policy console error.

## 2.2.0 — 2026-09-18

- Added persistent per-container alert muting from the inspector. Muted containers remain visible with their real Docker state, leave the needs-attention count, and no longer keep incidents active.
- Added Compose-project service cards with aggregate health and resource summaries.
- Added persistent service maintenance windows and opt-in HTTP endpoint monitoring.
- Added a 24-hour system brief, incident markers on telemetry charts and a global command palette.

## 2.1.0

- Persistent host telemetry: 1h, 24h and 7d views with explicit coverage and gaps.
- Durable local incidents with debounce, recovery, acknowledgement and seven-day retention.
- Generic host identity, optional hardware sensors and Docker API negotiation.
- Multi-stage Docker image, Compose setup, one-command launcher, backups and native systemd template.
- English installation, configuration, architecture, security and contribution documentation; CI and secret exclusions.

## 2.0.0

- Carbon default theme, presets and custom theme editor with per-device persistence.
- English interface, interactive topology, container pins, chart inspection and log follow/filter tools.
- Motion controls, reduced-motion support, responsive layout and a private 30-day session.
