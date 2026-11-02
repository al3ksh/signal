# Installation and recovery

## Docker Compose (recommended)

Install [Docker Engine](https://docs.docker.com/engine/install/) and the [Compose plugin](https://docs.docker.com/compose/install/linux/) using the official instructions for your distribution. Use a local Docker daemon on a Linux x86_64 or ARM64 host. The installing account must be allowed to use Docker.

```sh
sh ./signal start
```

The launcher checks Linux, Docker, Compose and the local socket, writes a private `.env`, builds a multi-stage image, initializes access once, and waits for the service health check. The first build needs network access to Docker Hub and npm and can take several minutes on an SBC. Runtime does not require internet access.

The image runs as UID 10001 with only the socket's supplementary group. Its application filesystem is read-only. `/data` is a persistent Docker volume. See [Security](../SECURITY.md) before mounting host resources.

Rerun the command after configuration changes. It preserves access and saved history. A failed health check leaves the container available for inspection with `sh ./signal logs`.

`COMPOSE_PROJECT_NAME=my-observatory sh ./signal start` creates a separate installation. Keep that project name for every subsequent command. Use a different `SIGNAL_PORT` for each installation. The default project name is `signal`.

## Native Python and systemd

This alternative needs Linux, Python 3.11+, access to the Docker socket, and Node.js 22.12+ for the build step only. The Python server uses the standard library.

Build on the server or copy the resulting `dist/` from another machine:

```sh
npm ci
npm run build
python3 setup_access.py data/access.json
python3 server.py
```

For a system service, copy `server.py`, `storage.py`, `alerts.py` and `dist/` to `/opt/signal`. Create a dedicated `signal` system account with the Docker socket group, and a writable `/var/lib/signal` directory owned by that account. Copy `setup_access.py` to `/opt/signal` for initialization:

```sh
sudo useradd --system --user-group --home-dir /opt/signal --shell /usr/sbin/nologin signal
sudo usermod -aG docker signal
sudo install -d -o signal -g signal -m 700 /var/lib/signal
sudo -u signal python3 /opt/signal/setup_access.py /var/lib/signal/access.json
sudo cp deploy/signal.service /etc/systemd/system/signal.service
sudo systemctl daemon-reload
sudo systemctl enable --now signal.service
```

If your socket uses another group, replace `docker` in the account setup and service. Package-manager paths may differ by distribution. The unit declares the writable data directory while keeping the application files read-only.

An existing installation can keep its old auth file by setting `SIGNAL_AUTH_FILE` explicitly. No password migration is required. If upgrading a hardened unit, grant write access to `SIGNAL_DATA_DIR` (e.g. `ReadWritePaths=/var/lib/signal`).

## Update and rollback

Before updating, run `sh ./signal backup` and keep the previous source revision or image tag. Update the source, then run `sh ./signal update`. The launcher rebuilds; it does not pull a repository or delete volumes. To roll back this version, restore the previous source and rebuild. A future database schema change may also require restoring the matching backup; review its release notes.

For native installations, deploy with `deploy/deploy.py` (below). Keep the access file and data directory outside the release files; the deploy script never touches them.

## Deploy to a native install

`deploy/deploy.py` is the single supported way to update a native (systemd) install from a workstation. It needs Python 3, `ssh` and, unless you pass `--skip-build`, Node.js; it works from Windows Git Bash, Linux and macOS. The host needs `sh`, `tar`, `sha256sum`, `flock`, `curl` and `sudo` (all present on Raspberry Pi OS). There is no rsync or scp: everything is one tar stream over ssh.

```sh
python deploy/deploy.py alexpi@100.105.170.124              # build, test, ship what changed, restart what needs it, verify
python deploy/deploy.py alexpi@100.105.170.124 --dry-run    # show what would change; touches nothing on the host
python deploy/deploy.py alexpi@100.105.170.124 --list       # releases kept on the host
python deploy/deploy.py alexpi@100.105.170.124 --rollback   # back to the state before the last deploy (run again to undo)
```

What a deploy does, in order:

1. **Preflight (local):** refuses a dirty working tree (`--allow-dirty`), runs `npm run build` (`--skip-build`) and `python -m unittest -q` (`--skip-tests`). What ships is derived from the files: every non-test `*.py` at the repository root plus `dist/`.
2. **Lock:** `flock` on `<dir>/.deploy.lock` for the whole session, so two deploys cannot overlap.
3. **Diff:** one `sha256sum` pass on the host decides what changed. A frontend change needs no restart; `runner.py` restarts only `signal-runner.service`; `setup_access.py`/`setup_2fa.py` restart nothing; any other module restarts `signal.service`. Before changing anything it checks `sudo -n -l systemctl restart <unit>`; if sudo would prompt, it stops and prints the sudoers line or the command to run by hand.
4. **Snapshot:** `releases/<UTC stamp>/` gets the host's current `dist/`, all its Python files and a `manifest.json`. The newest 5 are kept.
5. **Upload and swap:** a tar into `releases/<stamp>.incoming/`, hashes checked on the host, then `dist/` is swapped by rename and Python files are installed with `install -m 644`.
6. **Restart and verify:** only the affected units, then `/healthz` must answer within 30 s (`--verify-seconds`), the served `index.html` must reference the new `assets/index-*.js`, and restarted units must be `active`.
7. **Automatic rollback:** if verification fails, the snapshot is restored, the units restarted and verified again, and the script exits non-zero saying which state is running.

`--rollback [STAMP]` restores a snapshot (default: the newest) after taking a fresh snapshot of the current state, so running it twice returns to where you were. `--prune-legacy` additionally lists the old `dist-before-*`, `backend-before-*` and `release-*` directories with their sizes and deletes them after you confirm. `--dir` (or `SIGNAL_DEPLOY_DIR`) changes the install directory, default `/home/alexpi/apps/signal`.

To let restarts run without a password prompt, add a sudoers drop-in on the host (`sudo visudo -f /etc/sudoers.d/signal-deploy`):

```text
alexpi ALL=(root) NOPASSWD: /usr/bin/systemctl restart signal.service, /usr/bin/systemctl restart signal-runner.service
```

`deploy/update-signal.sh` remains as a thin wrapper around `deploy.py`.

## Free disk space on the Pi

`deploy/pi-clean.py` finds what can be removed and decides per item, never wholesale. Copy it to the Pi once (it is not part of a SIGNAL deploy) and run it there:

```sh
ssh alexpi@100.105.170.124 'cat > ~/pi-clean.py' < deploy/pi-clean.py
ssh -t alexpi@100.105.170.124 sudo python3 pi-clean.py            # scan: report only, changes nothing
ssh -t alexpi@100.105.170.124 sudo python3 pi-clean.py apply      # asks y / each / N per group
```

Every finding has a size, an age, a safety level and the exact action:

- **safe:** Docker build cache, dangling `<none>` images, journal above 100 MB, atop logs older than 7 days, the apt package cache, SIGNAL deploy backups beyond the newest five and `__pycache__` under `~/apps`. `apply --safe` applies only these, printing each first.
- **review:** images that no container (running or stopped) and no registered app's compose file uses, with `:signal-prev` rollback images of existing apps excluded; stopped containers older than 14 days with the image space they pin (a Compose project whose containers are all stopped is removed with `docker compose -p <project> down`, volumes kept); and the persistent config changes (journald `SystemMaxUse=100M`, atop `LOGGENERATIONS=7`), which are always separate items.
- **report-only:** `~/backups` and `~/apps/*backup*`, dangling volumes, and the 15 largest other files over 100 MB. The tool never removes these.

It never runs `docker system prune`, `docker image prune -a` or `docker volume prune`, and it ignores `docker system df`, whose reclaimable figure is wrong with the containerd image store. Each item is re-checked just before it is applied and skipped if it no longer qualifies; every action is logged with the space freed to `/var/log/pi-clean.log`. `--json` prints the scan for scripts.

## Backup and restore

`sh ./signal backup` briefly stops collection and writes `backups/signal-<UTC timestamp>.tar` with mode 600. It includes the whole data directory (SQLite, WAL if present, password hash and session secret). Treat it as private. Copy it off the monitored host. A stopped service creates a visible gap in history.

To restore a Docker backup on a prepared installation (substitute your file):

```sh
docker compose stop signal
docker compose run --rm --no-deps -T signal python -c \
  'import tarfile,sys; tarfile.open(fileobj=sys.stdin.buffer,mode="r|*").extractall("/",filter="data")' \
  < backups/signal-YOUR-TIMESTAMP.tar
docker compose up -d --wait
```

Only restore your own trusted backup. Restore into an empty data volume, or remove the old `signal.db`, `signal.db-wal` and `signal.db-shm` while the service is stopped before restoring, to avoid mixing database generations. Never delete a volume belonging to another application. The archive restores the original password and session-signing key.

## Forgotten password

Stop the service and back up private data first. Replace only `access.json`, then restart; the regenerated signing key invalidates all old sessions. For Docker:

```sh
docker compose stop signal
docker compose run --rm --no-deps signal python -c \
  'from pathlib import Path; from setup_access import create_access; p=Path("/data/access.json"); p.rename("/data/access.previous.json"); print(create_access(p))'
docker compose up -d --wait
```

Save the displayed password privately. If `access.previous.json` already exists, move that previous backup aside first. History is unaffected.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Docker unavailable | Socket path, socket permissions/group, daemon state; rerun `doctor` and `start` after socket group changes. |
| Port is in use | Choose another `SIGNAL_PORT` and adjust your reverse proxy. |
| No temperature | Common on VPSs and some PCs; no alert is generated from a missing sensor. |
| Some processes absent | `hidepid`, kernel permissions and non-root visibility; SIGNAL does not add privileged access to bypass these. |
| No network throughput | Host networking is required in Docker. Loopback and common Docker bridge/veth interfaces are excluded. |
| Healthy Docker but application unavailable | Check the HTTP health endpoint, backend logs and proxy route. Docker container state is not an HTTP availability probe. |
| Database cannot be opened | Ensure `/data` (Docker) or `SIGNAL_DATA_DIR` (native) is writable by the service account. |
| Rootless Docker | Set `DOCKER_SOCKET` to the real socket; test permissions and host mounts. Rootless/restricted environments are not verified configurations. |
