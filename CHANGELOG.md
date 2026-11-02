# Changelog

## Unreleased

- Added an optional app registry and a separate runner (`runner.py`) that reports each Compose app's deployed commit and how many commits it is behind its repository branch. A new Apps view appears only when `SIGNAL_CONTROL_SOCKET` is set and the runner is reachable.
- Added start, stop and restart for registered apps through the runner. Control is refused through the public tunnel, needs a same-origin request and a password step-up (10-minute grant), is rate-limited and is recorded in the journal. Ships a hardened `deploy/signal-runner.service`.
- Added one-click update from the repository (fast-forward only, refused on a dirty or diverged checkout) with live job logs, a phase indicator, container and HTTP health checks and automatic rollback to the exact previous commit, reusing the previous images when they were saved. Added manual rollback, job history, job cancellation and command-palette entries for updating, restarting and opening job logs.

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
