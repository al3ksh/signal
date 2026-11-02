# App control

App control turns SIGNAL from a read-only observatory into a small control plane for the Compose apps on one host. Per registered app it shows the running state, the deployed commit and how far it is behind its repository branch, and, from an allowed network, offers **Start**, **Stop**, **Restart**, **Update** and **Roll back** with live job logs.

It is **off by default**. Without `SIGNAL_CONTROL_SOCKET` SIGNAL looks and behaves exactly as before: no Apps view, no control routes (they answer 404), no runner.

## How it is put together

```text
browser ──HTTPS──> server.py (unprivileged, Docker GET only, as before)
                     │  {"op":"run","app":"tools","action":"update","actor":"session@100.x"}
                     ▼
                  /run/signal-runner/control.sock  (0660, newline-delimited JSON)
                     ▼
                  runner.py  (signal-runner.service, docker group)
                     │  /etc/signal/apps.json  (registry, validated at start, refuses to run if invalid)
                     │  git … / docker compose -p <id> -f … as argv lists, cwd = app dir, minimal env
                     ▼
                  /var/lib/signal-runner/  jobs.db · logs/<job>.log (2 MiB cap keeping the beginning and the latest output, last 200 jobs)
```

- The browser and API only ever send `{app, action}`. Apps come from the registry; actions are the fixed set `start`, `stop`, `restart`, `check`, `update`, `rollback`. No request field becomes part of a command line, and nothing runs through a shell.
- The web server never gets Docker write access. It asks the runner over the Unix socket.
- One job runs at a time per app (others queue); only one build or image pull runs at a time across all apps, to protect the Pi's RAM. Timeouts: lifecycle actions 120 s, builds and pulls 30 min, health per registry entry.
- Every app with a repository is fetched every 15 minutes (`SIGNAL_RUNNER_CHECK_INTERVAL`) to refresh "N commits behind". **Check now** does the same on demand.

**Trust boundary:** the runner is in the `docker` group, and docker group membership is equivalent to root on the host. Whoever can write to the runner socket can run the registered actions on the registered apps, and nothing else. Keep `/etc/signal/apps.json` root-owned (`0644`) and the socket group limited to the SIGNAL web user.

## Who may press the buttons

A mutation needs **all** of:

1. A private SIGNAL session (the access password login).
2. A request that did **not** come through Cloudflare. cloudflared connects from `127.0.0.1`, so SIGNAL treats any request carrying `Cf-Ray`, `CF-Connecting-IP`, `Cf-Visitor` or `Cf-IPCountry` as public. `status.aleksh.xyz` therefore stays read-only: it shows apps, commits and job history but no buttons ("Control is available over Tailscale or on the host"). `X-Forwarded-For` and `CF-Connecting-IP` are never used to grant access.
3. A socket peer inside `SIGNAL_CONTROL_ALLOW` — by default Tailscale (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`) and loopback (`127.0.0.1/32`, `::1/128`).
4. A same-origin JSON request (`Origin` equal to `Host`, or `Sec-Fetch-Site: same-origin`, and `Content-Type: application/json`).
5. A password step-up: the first action asks for the access password again and sets a 10-minute, HttpOnly, SameSite=Strict grant bound to the session. `check` (a `git fetch`) does not need the step-up.

Requests are rate-limited (20 control requests and 5 failed step-ups per minute per client). Every control request, accepted or refused, is written to the service log (`journalctl -u signal`); those from signed-in sessions also appear in the Signal journal (kind `control`). Anonymous refusals stay out of the journal so they cannot push real events out of it.

If you add another reverse proxy on the Pi that carries public traffic to `127.0.0.1:8091` (Caddy, nginx), remove the loopback ranges from `SIGNAL_CONTROL_ALLOW`, otherwise public requests through it would look local.

### Reaching SIGNAL over Tailscale

SIGNAL binds to `127.0.0.1`. The simplest way to reach it from your tailnet with HTTPS is Tailscale Serve, which proxies from loopback without Cloudflare headers:

```sh
sudo tailscale serve --bg --https=8443 http://127.0.0.1:8091
```

Then open `https://<pi>.<tailnet>.ts.net:8443`. Alternatively bind `SIGNAL_HOST=100.105.170.124` and use `http://100.105.170.124:8091` (the session cookie is then not marked Secure).

## Install on the Pi

These steps assume the native install described in [INSTALLATION.md](INSTALLATION.md) at `/home/alexpi/apps/signal`, running as `alexpi`.

1. **Update SIGNAL** (frontend and Python) from your workstation:

   ```sh
   deploy/update-signal.sh alexpi@100.105.170.124
   ```

2. **Create the registry** from [`apps.example.json`](../apps.example.json). It already matches the host's table: `portfolio`, `discord-bot`, `tools`, `medium` with repositories; `ai-browser`, `lights-out`, `bread-power-dashboard` lifecycle-only. Check each `repo.branch` (assumed `main`), the compose file list (add `compose.landing-*.yml` for `discord-bot` if those belong to the same project) and the health URLs (only `portfolio` has one; the others rely on container state until you add one).

   ```sh
   sudo install -d -m 0755 /etc/signal
   sudo install -m 0644 -o root -g root /home/alexpi/apps/signal/apps.example.json /etc/signal/apps.json
   sudoedit /etc/signal/apps.json
   ```

   Rules: `id` is `[a-z0-9-]{1,40}` and must equal the Compose project name SIGNAL already shows; `dir` is absolute; compose files are relative and must exist inside `dir`; `repo` is optional (`remote` default `origin`, `branch` default `main`); `health` is optional (`url`, `expect` default 200, `timeoutSeconds` 1–900, default 60 when omitted); `update.mode` is `build` (default) or `pull`. The runner refuses to start on any invalid entry and says which.

3. **Install the runner unit** and start it:

   ```sh
   sudo cp /home/alexpi/apps/signal/deploy/signal-runner.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now signal-runner.service
   journalctl -u signal-runner -n 20   # "SIGNAL runner managing 7 apps on /run/signal-runner/control.sock"
   ```

4. **Point SIGNAL at the socket**: `sudo systemctl edit signal.service` and add

   ```ini
   [Service]
   Environment=SIGNAL_CONTROL_SOCKET=/run/signal-runner/control.sock
   # optional, these are the defaults:
   # Environment=SIGNAL_CONTROL_ALLOW=100.64.0.0/10,127.0.0.1/32,::1/128,fd7a:115c:a1e0::/48
   ```

   If `signal.service` uses `ProtectSystem=strict`/`ReadWritePaths`, connecting to a socket under `/run/signal-runner` still works (it is not a file write). Then `sudo systemctl restart signal.service`.

5. Open SIGNAL over Tailscale. **Apps** appears in the navigation. Try **Check now**, then **Restart** on a lifecycle-only app before the first **Update**.

To turn control off again, remove `SIGNAL_CONTROL_SOCKET` and restart `signal.service`; stop the runner with `sudo systemctl disable --now signal-runner`.

## What each action does

All commands run with `cwd` set to the app directory and `docker compose --project-directory <dir> -p <id> -f <file>…`.

| Action | Steps |
| --- | --- |
| Start | `up -d` |
| Stop | `stop` |
| Restart | `restart` |
| Check now | `git fetch --no-tags <remote> <branch>`, then compare: deployed commit, commits behind (up to 20 listed), ahead, current branch, local changes. |
| Update | fetch → refuse if not on the configured branch, in pull mode refuse while CI has not published the images for the new commit, if tracked files have local changes, or if the checkout has commits the remote does not (only fast-forwards) → no-op if already current → tag the images of the running commit `<image>:signal-prev` → `git merge --ff-only` → **build** (`build --pull`, only changed services, see below) or **pull** → `up -d --remove-orphans` → health. |
| Roll back | Returns to the commit that was running before the last **successful** update (`git reset --hard` to that exact recorded commit), restores its `:signal-prev` images when they are still the latest saved ones (otherwise rebuilds in build mode), `up -d`, health. |

**Health** passes when every project container is running and not `unhealthy`/`starting` (exited containers with exit code 0 count as finished one-shot services), and, if configured, the health URL answers the expected status. It is polled every 2 s until `timeoutSeconds`.

**Incidents:** a failed job opens a critical incident and a rolled-back one a warning in **Alerts & events** (`deploy:<app>`), so an automatic rollback is never silent. The next successful job for that app resolves it; a Services maintenance window for the app suppresses it.

**Recovery after a restart:** a deploy interrupted after the checkout moved is rolled back automatically when the runner starts again (see Troubleshooting).

**Automatic rollback:** if anything fails after the checkout moved — build, pull, `up`, health, or you press **Cancel** — the runner resets to the exact previous commit, restores the saved images (or rebuilds when none were saved), runs `up -d` and checks health again. The job ends as `rolled back` with the reason, or `failed` if the rollback itself failed (the log says why). Untracked files (`.env`, data folders) are never touched.

Every compose command receives `SIGNAL_COMMIT=<deployed commit sha>` in its environment.

## Faster updates

Ordered by impact. Building on the Pi is the slow part: `npm ci` and image layer writes on an SD card take minutes and compete with the running apps for RAM.

### 1. Build images in CI, deploy by pulling (`update.mode: "pull"`)

Copy [`templates/build-arm64.yml`](templates/build-arm64.yml) into an app repository as `.github/workflows/build-arm64.yml`. On every push to `main` it builds `linux/arm64` natively on `ubuntu-24.04-arm`, caches layers with `type=gha`, and pushes `ghcr.io/al3ksh/<repo>:<sha>` and `:main`.

Then in the app's compose file:

```yaml
services:
  web:
    image: ghcr.io/al3ksh/portfolioos:${SIGNAL_COMMIT:-main}
    # keep `build:` only if you still want to be able to build locally
```

and in `/etc/signal/apps.json` set `"update": {"mode": "pull"}`. Update now runs `docker compose pull` + `up -d`: seconds instead of minutes. Because the runner passes the deployed commit as `SIGNAL_COMMIT`, the Pi pulls exactly the image built for that commit, and a rollback returns to the previous commit's image (still local, re-tagged from `:signal-prev`). Before moving the checkout, the runner asks the registry (`docker manifest inspect`) whether every image pinned to the new commit exists. If the Actions run has not finished yet, the update stops with "…is not published yet" and nothing changes; press **Update** again once CI is green.

Private packages: on the Pi run `docker login ghcr.io` as `alexpi` once with a token that has `read:packages`. The runner reads `~/.docker/config.json`.

### 2. Dockerfile hygiene (app repositories)

- Copy lockfiles before sources so dependency layers stay cached: `COPY package.json package-lock.json ./` then `RUN npm ci`, then `COPY . .`.
- Use BuildKit caches: `RUN --mount=type=cache,target=/root/.npm npm ci` (pip: `--mount=type=cache,target=/root/.cache/pip`).
- Add a `.dockerignore` with at least `node_modules`, `.git`, `dist`, `build`, `.env*`.
- Multi-stage builds: build in a `node:22` stage, ship only the output in a small runtime stage.
- Pin base images (`node:22.11-alpine`, or by digest) so `--pull` does not invalidate every layer unexpectedly.

### 3. Skip unchanged services (build mode, automatic)

In build mode the runner compares `git diff --name-only <prev> <new>` with each service's build context and Dockerfile (from `docker compose config`, read without logging because it contains interpolated `.env` values). Only services whose context changed are built; a change only to docs or other services skips the build entirely and just runs `up -d`. Any change to a registered compose file, an unreadable config, a remote build context or an unusual service name falls back to building everything.

### 4. Hardware

Move Docker's data root to a USB 3 SSD (`"data-root"` in `/etc/docker/daemon.json`, stop Docker, `rsync -aHAX` the old root, start Docker). The SD card is the bottleneck for `npm ci` and image layers, and SSDs survive the write load much better.

### 5. SIGNAL itself

SIGNAL is not Compose-managed on this host and the runner does not manage it. Use [`deploy/update-signal.sh`](../deploy/update-signal.sh) from a workstation or CI: it builds the frontend locally, keeps `dist-before-<timestamp>` backups on the Pi (last 5), syncs `dist/`, syncs Python files only when they changed, and restarts `signal.service` (or `signal-runner.service` for `runner.py`) only in that case. It prints the one-line rollback command for the frontend.

## Runner configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `SIGNAL_RUNNER_REGISTRY` | `/etc/signal/apps.json` | App registry. |
| `SIGNAL_RUNNER_DATA_DIR` | `/var/lib/signal-runner` | `jobs.db` and job logs. |
| `SIGNAL_RUNNER_SOCKET` | `/run/signal-runner/control.sock` | Control socket (created `0660`). |
| `SIGNAL_RUNNER_CHECK_INTERVAL` | `900` | Seconds between automatic repository checks. |

Web server: `SIGNAL_CONTROL_SOCKET` (unset = control off) and `SIGNAL_CONTROL_ALLOW` (comma-separated CIDRs allowed to mutate).

## Troubleshooting

- **No Apps entry:** `SIGNAL_CONTROL_SOCKET` is unset, or the runner is not running (`systemctl status signal-runner`). `/api/session` reports `control.available`.
- **"Read-only here" on the Tailscale address:** check that the request is not going through Cloudflare and that the peer address is in `SIGNAL_CONTROL_ALLOW` (`journalctl -u signal` shows refused control requests with the client address).
- **Update refused, "local changes":** someone edited tracked files on the Pi. Inspect with `git -C <dir> status`, then commit/push, stash or discard.
- **Update refused, "diverged":** the Pi checkout has commits that are not on the remote. Push them or reset the checkout deliberately; the runner only fast-forwards.
- **Permission denied on `.git`:** the app directory must be writable by the runner user (`alexpi`) and inside `ReadWritePaths`.
- **Job interrupted:** a runner restart (service stop, reboot) marks unfinished jobs as failed. If a deploy was cut off after its checkout moved, the runner starts a **Recover** job on start-up that resets to the commit the deploy started from, restores its saved images, runs `up -d` and checks health. It does this only while the checkout is still exactly where the interrupted job left it; if someone changed it by hand, the runner leaves it alone and logs why.
