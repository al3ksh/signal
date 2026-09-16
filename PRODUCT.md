# SIGNAL product brief

SIGNAL is a private, read-only observatory for one Linux Docker host. It helps the owner check application state and host load without opening a full container administration console.

The interface is technical and expressive: graphite by default, sharp typography, compact telemetry and an interactive orbital map. Custom themes and motion are preferences stored in the browser. Operational data remains on the host.

Supported deployment targets are Linux x86_64 and ARM64, including Raspberry Pi OS 64-bit. Hardware-specific sensors are optional. No personal hostname, domain, tunnel identifier or server path belongs in the public source.

Core tasks: sign in, inspect live host readings and containers, filter/pin applications, read bounded logs, inspect saved telemetry, review and acknowledge incidents. Docker management, cross-host orchestration and external notification delivery are outside the current release.

The repository includes a Docker Compose launcher and a native Python/systemd alternative. React/TypeScript is built into static assets; the backend uses Python's standard library and SQLite.
