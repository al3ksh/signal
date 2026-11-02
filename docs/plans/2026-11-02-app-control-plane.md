# Plan: SIGNAL app control plane (start / stop / update from repo)

Status: proposed · 2026-11-02 · Written to be executed by a cloud Claude session that has this repository but **no access to the Raspberry Pi**.

## Goal

Turn SIGNAL from a read-only observatory into a small, safe control plane for the Compose apps on the owner's Raspberry Pi:

1. See, per app: running state, deployed commit/version, and whether the repo has newer commits.
2. One-click **start / stop / restart** of an app.
3. One-click **update from repo** with live build logs, a health check and **automatic rollback**.
4. Make updates fast by building images in CI instead of on the Pi.

The public observatory must keep working exactly as today. Control is an add-on that is **off by default**.

## Ground truth about the target host (measured 2026-11-02)

- Raspberry Pi, `aarch64`, 3.7 GiB RAM, root on a 29 GB **SD card** (`mmcblk0`), no SSD. Docker 29.2.1, Compose v5.0.2.
- SIGNAL runs **outside Docker** as `signal.service` (user `alexpi`, `/home/alexpi/apps/signal`, `python3 server.py`, `SIGNAL_HOST=127.0.0.1`, `SIGNAL_PORT=8091`, `SIGNAL_AUTH_FILE=/home/alexpi/.config/signal/access.json`, `SIGNAL_DATA_DIR=/var/lib/signal`). Public via Cloudflare Tunnel at `status.aleksh.xyz`. The Pi is also on Tailscale (`100.105.170.124`).
- Apps live in `/home/alexpi/apps/<dir>`; Compose project labels match:

| Compose project | Directory | Repo | Compose file(s) |
|---|---|---|---|
| `portfolio` | `apps/portfolio` | github.com/al3ksh/PortfolioOS | `docker-compose.yml` + `docker-compose.override.yml` |
| `discord-bot` | `apps/discord-bot` | github.com/al3ksh/BreadMusic | `docker-compose.yml` (+ `compose.landing-*.yml`) |
| `tools` | `apps/tools` | github.com/al3ksh/Tools | `docker-compose.yml` |
| `medium` | `apps/Medium` | github.com/al3ksh/Medium | `docker-compose.prod.yml` |
| `ai-browser` | `apps/ai-browser` | none | `docker-compose.yml` |
| `lights-out` | `apps/lights-out` | none | `docker-compose.yml` |
| `bread-power-dashboard` | `apps/bread-power-dashboard` | none | `docker-compose.power.yml` |

Portainer also runs on the host; leave it alone.

## Non-negotiable boundaries

- **No arbitrary command execution.** The UI and API only send `{app, action}`; the set of apps comes from a registry file on the host, the set of actions is a fixed enum. No shell strings are built from request data (`subprocess` with argv lists only, `shell=False`).
- **The web process never gets write access to Docker.** Mutations go through a separate runner (below). The web server keeps its current read-only socket usage.
- **Control is disabled unless explicitly configured** (`SIGNAL_CONTROL_SOCKET` set and the runner reachable). With it unset, SIGNAL behaves and looks exactly as today; all existing tests must pass unchanged.
- **Control endpoints are not usable from the public hostname by default.** Accept control requests only when the request arrives over an allowed path (see Security). Read-only "update available" info may be shown publicly.
- Keep the existing code style (dense TSX/CSS, stdlib-only Python, `unittest`). No new Python dependencies. No new heavy npm dependencies.
- Update `DESIGN.md` "Boundaries" and `docs/ARCHITECTURE.md` to describe the new, deliberate exception, and `CHANGELOG.md`.

## Architecture

```
browser ──HTTPS──> server.py (user alexpi, unprivileged)
                     │  read-only: /proc, docker.sock GET (as today)
                     │  control: JSON over unix socket
                     ▼
                  runner.py (systemd: signal-runner.service, user with docker group)
                     │  reads /etc/signal/apps.json (registry, root-owned, 0644)
                     │  git / docker compose via argv lists
                     ▼
                  /var/lib/signal-runner/  (jobs.db, logs/<job>.log, state)
```

### 1. App registry — `apps.json` (host file, not in repo; ship `apps.example.json`)

```json
{
  "apps": [
    {
      "id": "portfolio",
      "name": "PortfolioOS",
      "dir": "/home/alexpi/apps/portfolio",
      "composeFiles": ["docker-compose.yml", "docker-compose.override.yml"],
      "repo": {"remote": "origin", "branch": "main"},
      "health": {"url": "http://127.0.0.1:8080/", "expect": 200, "timeoutSeconds": 90},
      "update": {"mode": "build"}
    }
  ]
}
```

- `id`: `[a-z0-9-]{1,40}`, must match the Compose project name so SIGNAL can join registry entries with containers it already sees.
- `repo` optional (apps without git get start/stop/restart only).
- `update.mode`: `"build"` (git pull + `compose build` on the Pi) or `"pull"` (git pull + `compose pull`, images built in CI; phase 4).
- Runner validates the file at start and refuses to run with an invalid registry; `dir` must be absolute and exist; compose files must exist inside `dir` (resolve and check no `..` escape).

### 2. Runner — `runner.py` (new, stdlib only)

- Listens on a unix socket (default `/run/signal-runner/control.sock`, mode `0660`, group shared with the web user). Newline-delimited JSON request/response; no HTTP.
- Requests: `list`, `status {app}`, `run {app, action, actor}`, `job {id}`, `log {id, offset}`, `cancel {id}`.
- Actions enum: `start`, `stop`, `restart`, `check` (git fetch + compare), `update`, `rollback`.
- One job at a time per app (lock); a global limit of 1 concurrent `build` to protect the Pi's RAM. Queue others.
- Every job: id, app, action, actor, started/finished, exit status, previous commit, new commit, log file. Stored in SQLite `jobs.db` (reuse the patterns from `storage.py`); logs in `logs/<id>.log`, capped at ~2 MB each, keep the last 200 jobs.
- Timeouts: start/stop/restart 120 s, build 30 min, health check per registry.
- Commands (always `cwd=dir`, argv lists, `env` minimal + `GIT_TERMINAL_PROMPT=0`):
  - `check`: `git fetch --quiet <remote> <branch>`; `git rev-parse HEAD`; `git rev-list --count HEAD..<remote>/<branch>`; `git log --format=%h%x09%s%x09%an%x09%ct HEAD..<remote>/<branch> -n 20`; `git status --porcelain` (dirty tree ⇒ report, block update).
  - `update` (mode build):
    1. refuse if working tree is dirty or the branch has diverged (only fast-forward);
    2. record `prev = git rev-parse HEAD`;
    3. `git merge --ff-only <remote>/<branch>`;
    4. `docker compose -f … build --pull`;
    5. `docker compose -f … up -d --remove-orphans`;
    6. health check (HTTP expect status, plus all project containers `running` and not `unhealthy`), polling until timeout;
    7. on failure at 4–6: `git reset --hard <prev>` (only ever to the recorded commit), rebuild/up, mark job `rolled_back`, keep the log.
  - `rollback`: same as the failure path, to the previous successful deployed commit recorded in `jobs.db`.
  - `start`/`stop`/`restart`: `docker compose -f … up -d` / `stop` / `restart`.
- Before updating, tag current images as `<image>:signal-prev` so a rollback in `pull` mode is instant.
- Periodic `check` for every app with a repo every 15 minutes (configurable), results cached for the web server.
- Ship `deploy/signal-runner.service` (hardened: `NoNewPrivileges`, `ProtectSystem=strict`, `ReadWritePaths=/var/lib/signal-runner /home/alexpi/apps`, `RuntimeDirectory=signal-runner`, `SupplementaryGroups=docker`). Document that docker group == root and that this is the trust boundary.

### 3. Web server changes — `server.py`

- New optional env: `SIGNAL_CONTROL_SOCKET`, `SIGNAL_CONTROL_ALLOW` (comma-separated CIDRs allowed to *mutate*, default `100.64.0.0/10,127.0.0.1/32` = Tailscale + local).
- Determine client address from the socket peer. Do **not** trust `X-Forwarded-For` / `CF-Connecting-IP` for authorization; requests arriving through Cloudflare Tunnel come from `127.0.0.1` via cloudflared, so detect tunnel traffic by the presence of `Cf-Ray`/`CF-Connecting-IP` headers and treat it as **public → read-only**. (Document this clearly; add a test.)
- New endpoints (all behind existing session auth):
  - `GET /api/apps` → registry entries joined with container state + latest `check` result + last job. Public-safe (no paths? keep `dir` out of the response).
  - `GET /api/apps/<id>/jobs`, `GET /api/jobs/<id>`, `GET /api/jobs/<id>/log?offset=` (text, incremental; the UI polls every 1 s while running — keep it simple, no SSE needed).
  - `POST /api/apps/<id>/<action>` → requires: control enabled, allowed network, CSRF check (same-origin `Origin`/`Sec-Fetch-Site`, reuse the existing POST protection if present, otherwise add it), and **step-up auth**: body must contain the access password, verified with the existing PBKDF2 check; on success mint a short-lived (10 min) "control" grant in the session so several actions don't each need the password.
  - Rate limit mutations (reuse the login limiter pattern).
- Audit: every mutation request (allowed or refused) is appended to the existing journal/alerts history as an event (`kind: "control"`), so it shows up in the UI timeline.
- `/api/session` additionally returns `control: {available: bool, allowed: bool}` so the UI knows what to render.

### 4. UI

- New view **Apps** (next to Services; or extend `Services.tsx` if it fits better — decide after reading it). Per app card: name, state, deployed short commit + age, "N commits behind" badge with expandable commit list, last job result.
- Buttons Start / Stop / Restart / Update / Rollback shown only when `control.allowed`; otherwise a quiet note "Control available over Tailscale". Disabled while a job runs.
- Confirm dialog for every action: shows what will happen (e.g. the commits to be deployed); first action asks for the password (step-up).
- Job drawer: live log tail (monospace, autoscroll, follow toggle — reuse the existing logs component patterns), phase indicator (fetch → build → up → health → done / rolled back).
- Command palette (`Ctrl+K`) entries: "Update <app>", "Restart <app>", "Open job log".
- Respect reduced motion; follow `DESIGN.md` tokens; no new colors.

### 5. Faster updates (phase 4)

Order of impact; implement 4a in this repo as docs + templates, the rest as recommendations in `docs/APP-CONTROL.md`:

- **4a. Build images in CI, not on the Pi.** Provide a reusable GitHub Actions workflow template (`docs/templates/build-arm64.yml`) for app repos: on push to `main`, build `linux/arm64` on `ubuntu-24.04-arm` runners with `docker/build-push-action`, cache with `type=gha`, push to `ghcr.io/al3ksh/<app>:<sha>` and `:main`. App compose files then reference the image; registry `update.mode: "pull"`; the runner deploys by `compose pull && up -d` (seconds instead of minutes). Rollback = re-tag `:signal-prev`.
- **4b. Dockerfile hygiene checklist** for app repos: copy lockfiles before sources, `RUN --mount=type=cache,target=/root/.npm npm ci` (same for pip), `.dockerignore` (`node_modules`, `.git`, `dist`), multi-stage builds, pinned base images.
- **4c. Skip unchanged services**: in `build` mode only rebuild services whose build context changed between `prev..new` (`git diff --name-only`), else `up -d` only.
- **4d. Hardware**: move Docker data root to a USB 3 SSD (documented recommendation; the SD card is the bottleneck for `npm ci` and image layers).
- **4e. SIGNAL itself** is not Compose-managed on this host; add `deploy/update-signal.sh` that builds the frontend locally/CI and rsyncs `dist/` (+ python files when changed), keeping `dist-before-<ts>` backups and restarting the service only when Python changed. This mirrors the manual process used today.

## Phases & acceptance

Ship each phase as its own PR; each must keep `npm run build` and `python -m unittest discover -v` green.

1. **Read-only awareness** — registry, runner with `list/status/check` only, `/api/apps`, Apps view with "commits behind". Acceptance: with control disabled nothing changes; with a fake runner the view renders states and badges.
2. **Lifecycle** — start/stop/restart via runner, network gating, step-up auth, CSRF, audit events, confirm dialogs.
3. **Update + rollback** — update/rollback jobs, live logs, health check, automatic rollback, job history.
4. **Speed** — CI template, pull mode, skip-unchanged, `update-signal.sh`, `docs/APP-CONTROL.md`.

## Testing (the cloud session cannot reach the Pi)

- Unit-test the runner against **temporary git repos** (create bare remote + clone in `tempfile`, make commits) and a **fake `docker` executable** placed first on `PATH` that records argv and can be told to fail build/up/health. Cover: dirty tree refused, diverged branch refused, ff update, build failure ⇒ rollback to exact `prev`, health timeout ⇒ rollback, per-app lock, global build limit, log capping, registry validation (bad id, path escape, missing compose file).
- Server tests (extend `test_server.py` style): control disabled ⇒ endpoints 404 and `/api/session` says unavailable; public/tunnel request ⇒ 403 on mutation; Tailscale/local ⇒ requires step-up; wrong password ⇒ 403 + rate limit; CSRF missing ⇒ 403; audit event written for allowed and refused attempts; app id not in registry ⇒ 404; no request field ever reaches argv except the validated id/action.
- Frontend: `npm run build` (tsc) must pass; check the Apps view in a browser preview against a small stub API, desktop and 375 px mobile.
- Do **not** attempt to SSH anywhere or deploy. Finish with a PR per phase and a short "how to install on the Pi" section in `docs/APP-CONTROL.md` (create `/etc/signal/apps.json` from the table above, install `signal-runner.service`, set `SIGNAL_CONTROL_SOCKET` in `signal.service`, restart).

## Open questions for the owner (defaults in brackets)

- Allow control from the public hostname at all? [no — Tailscale/local only]
- Step-up with the access password or a separate control password/TOTP? [access password, 10-min grant]
- Include apps without a git repo? [yes, lifecycle only]
- Should the runner also manage SIGNAL itself? [no — separate `update-signal.sh`]
