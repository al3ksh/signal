# Development

Use Node.js 22.12+ and Python 3.11+. Backend collection requires Linux; isolated unit tests and frontend development also run on Windows/macOS.

```sh
npm ci
python3 -m unittest discover -v
npm run build
```

To work against a local Linux host, initialize `data/access.json` with `python3 setup_access.py`, run `python3 server.py`, then `npm run dev` in a second shell. Vite proxies `/api` to `127.0.0.1:8091`. Do not expose the Vite development server publicly.

Tests cover sessions, access boundaries, log framing, origin checks, history aggregation/retention, persistence, debounce, recovery, missing sensors and Docker outages. Docker smoke testing should use a separate Compose project and port; never stop or modify existing application containers to manufacture a test incident.

Keep all Docker operations read-only. Add meaningful tests for persistence and security behavior. Use fixture data for failure scenarios. New host-specific features must degrade gracefully when the hardware is absent. Preserve keyboard navigation, reduced-motion preferences and responsive behavior.

The UI uses shared color tokens, Carbon as the default theme and an appearance editor for per-browser preferences. See [DESIGN.md](DESIGN.md). Include desktop and narrow-screen checks when modifying layout. Do not commit private screenshots or local infrastructure details.

Before publishing: choose a license, set the repository description and security contact, review `git diff --cached`, and run CI-equivalent checks. No remote repository or package publication is configured automatically.
