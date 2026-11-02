# Pi health, a real deploy script, and a semi-automatic disk cleaner

Status: planned (2026-11-02). Three independent workstreams; each lands as its own PR against `main`.

## Read this first (for whoever implements it)

- **You have no access to the Pi.** It sits on a Tailscale address that cloud runners cannot reach. Build and
  test everything locally with fixtures and fakes. Everything that needs the real host goes into the PR
  description under **"Verify on the Pi"**; the owner runs that part after merging.
- Python is stdlib only (the Pi runs Python 3.11), the same as `server.py` and `runner.py`. Tests use `unittest`
  and live next to the code (`test_*.py`). Run `python3 -m unittest -q` and `npm run build` before pushing.
- Match the existing style: compact code, short doc comments that explain *why*, and no new dependencies.
- Never add anything that deletes data from the web UI. SIGNAL's web side stays read-only plus the narrow
  runner actions documented in `docs/APP-CONTROL.md`.

## Facts about the host (measured 2026-11-02)

| | |
|---|---|
| Hardware | Raspberry Pi 4 Model B Rev 1.4, 3.7 GiB RAM, zram swap, 32 GB SD card (`mmcblk0`) |
| Root fs | `/dev/mmcblk0p2` ext4, 29 G, 15 G used (52 %), mounted `rw,noatime` |
| SIGNAL install | native systemd, `signal.service` runs as **alexpi** (groups include `video`, `docker`, `adm`), `ProtectSystem=strict`, files in `/home/alexpi/apps/signal` (no git checkout there), data in `/var/lib/signal` |
| Runner | `signal-runner.service`, data in `/var/lib/signal-runner` |
| Throttling | `vcgencmd get_throttled` works for alexpi (`throttled=0x0`); `/sys/devices/platform/soc/soc:firmware/get_throttled` does **not** exist on this kernel |
| SD health | no `life_time`/eMMC attributes (consumer SD). `/sys/fs/ext4/mmcblk0p2/errors_count` = 0 and `lifetime_write_kbytes` ≈ 508 M (~484 GB) are readable without root |
| PSI | `/proc/pressure/*` absent (kernel without `psi=1`); treat as optional |
| Clock | `/run/systemd/timesync/synchronized` exists when NTP is synced. The TOTP second factor depends on the clock |
| Docker | containerd image store (`/var/lib/containerd` 9.4 G, `/var/lib/docker` only 94 M). `docker system df` reports images as "99 % reclaimable" while 16 of 17 are in use, so **that number is wrong here and must not be used** |
| Disk hogs | images 10 G (`linuxserver/chromium` 4.5 G used only by the stopped `ai-browser`); `/var/log/atop` 223 M; journal 121 M; apt cache 115 M; `~/backups` 277 M; `~/apps/*backup*` ~150 M |
| Stopped containers | `ai-browser`, `ai-browser-proxy`, `ai-browser-speedtest`, `medium-server`, `medium-client`, all exited ~6 weeks ago |
| Deploy leftovers | `~/apps/signal` has 39 `backend-before-*`, `dist-before-*` and `release-*` dirs |
| Rollback tags | the runner tags `<image>:signal-prev` before updates (`runner.py` `save_images`). Those images are needed for rollback |

---

## 1. Pi health ("is the Raspberry OK?")

Today `server.py` reads temperature, `vcgencmd get_throttled` (as one int) and disk usage. The UI only says
"Hardware throttling detected" when one of the low 4 bits is set. That misses the sticky "since boot" bits, which
are the ones that actually explain random crashes (a weak PSU).

### Backend: new `health.py` (pure functions, root-relative for tests)

Every reader takes `proc_root`/`sys_root`/`run_root` (as `server.read` already does with `PROC_ROOT`/`SYS_ROOT`),
so tests can point it at a fixture tree.

1. **Power and throttling**: decode all bits of `get_throttled`.
   - Now: 0 under-voltage, 1 ARM freq capped, 2 throttled, 3 soft temp limit.
   - Since boot: 16–19, the same four.
   - Output: `{"now": [...names], "sinceBoot": [...names], "raw": "0x50005"}`.
   - Read the sysfs file when it exists, otherwise fall back to `vcgencmd`.
   - Run `vcgencmd` at most once per 30 s, not every 5 s sample.
2. **Core voltage**: `vcgencmd measure_volts core`, at the same cadence. Informational only.
3. **SD card**, from `/sys/fs/ext4/<dev>/`. Find the device from `/proc/mounts` for `/` (or `SIGNAL_DISK_PATH`).
   - `errors_count`, `first_error_time`, `last_error_time`.
   - `lifetime_write_kbytes`: store a daily snapshot in the `metadata` table so the UI can show "writes/day"
     (wear indicator) over the last 7 days.
4. **Read-only root**: `/proc/mounts` flags for `/`. `ro` means the card is failing or fsck kicked in, which is
   critical.
5. **Inodes**: `os.statvfs` → `f_files`/`f_ffree` alongside the existing disk numbers.
6. **Clock sync**: whether `/run/systemd/timesync/synchronized` exists. If it is missing, the 2FA codes may be
   rejected, so say that in the UI text.
7. **Memory pressure without PSI**:
   - zram from `/sys/block/zram*/mm_stat` (orig vs compressed) and swap use, already in `/proc/meminfo`;
   - major page faults per second from `/proc/vmstat` `pgmajfault`, as a delta.
   - If `/proc/pressure/{cpu,memory,io}` exists, include `avg60`. Otherwise omit the field (no zeros).
8. **Failed systemd units**: `systemctl list-units --state=failed --plain --no-legend --no-pager` with a 2 s
   timeout, every 60 s. It works without root over D-Bus; if it fails, report `null`, not an error.
9. **Disk forecast**: add `disk` (used %) to the history `point` in `Monitor.sample` and `storage.py`. Compute
   "full in N days" with a linear fit over the last 7 days, shown only when the slope is positive and N < 60.

`Monitor.sample` attaches the result as `data['health']`. Slow readers (vcgencmd, systemctl) run on their own
cadence and are cached, so they never block the 5 s sample.

### Alerts (`alerts.py`, reuse the debounced engine)

| key | condition | severity |
|---|---|---|
| `power:undervoltage` | under-voltage bit **now** | critical |
| `power:history` | any since-boot bit set (open once per boot; resolve on reboot) | warning, message "the PSU or cable may be too weak" |
| `sd:errors` | `errors_count` increased | critical |
| `fs:readonly` | root mounted `ro` | critical |
| `clock:unsynced` | not synced for 10 min | warning |
| `units:failed` | one or more failed units (names in the message) | warning |
| `disk:forecast` | full in < 14 days | warning |

The existing temperature and disk-90 % alerts stay as they are.

### Frontend

- A "Raspberry" health card (or panel next to the metrics row, matching existing `metric` cards in `main.tsx`)
  with one row per check: green, amber or red dot, a label and a short reason. Examples: "Power: OK since boot",
  "SD: 0 errors · ~1.2 GB written/day", "Clock: synced", "Failed units: none", "Disk: full in ~40 days".
- Replace the single "Hardware throttling detected" line on the temperature card with the decoded wording.
- Unknown values show "unavailable", never a fake OK.

### Tests

`test_health.py` with a fixture tree in a temp dir:
- throttle decoding for `0x0`, `0x50000`, `0x50005` and `0x80008`;
- an ext4 stats dir, `/proc/mounts` with `ro` and with `rw`, an absent PSI dir, and a zram `mm_stat` line;
- the forecast from synthetic points: flat, growing, and shrinking;
- alert open and resolve transitions in `test_history.py` style.

### Verify on the Pi
`curl -s 127.0.0.1:8091/api/...` (the authenticated snapshot) shows `health` with real values, and the card renders
on status.aleksh.xyz.

---

## 2. Deploy script (replace manual tar-over-ssh)

`deploy/update-signal.sh` exists but:
- needs `rsync`, which the owner's Windows/Git Bash machine does not have;
- has a stale `PYTHON_FILES` list that misses `setup_2fa.py`;
- never checks health and never rolls back by itself;
- backs up only `dist`.

In practice deploys have been done by hand, which left 39 backup dirs behind.

### `deploy/deploy.py` (stdlib, runs on Windows Git Bash, Linux and macOS)

```
python deploy/deploy.py alexpi@100.105.170.124            # build, ship, restart what changed, verify
python deploy/deploy.py HOST --dry-run                      # show what would change, touch nothing
python deploy/deploy.py HOST --skip-build                   # dist/ already built
python deploy/deploy.py HOST --list                         # releases on the host
python deploy/deploy.py HOST --rollback [STAMP]             # back to the previous (or named) release
```

The only transport is `ssh` (plus `tar`, which both ends have) through `subprocess`, with stdin and stdout
streaming. No rsync and no scp.

Steps:
1. **Preflight (local)**:
   - working tree clean, or `--allow-dirty`;
   - `npm run build` unless `--skip-build`;
   - `python -m unittest -q` unless `--skip-tests`.
   - The manifest is every non-test `*.py` at the repo root plus `dist/`. Derive it from the files, never from a
     hard-coded list.
2. **Lock**: run `flock -n <dir>/.deploy.lock` remotely for the whole session, so two deploys cannot overlap.
3. **Diff**: one ssh call runs `sha256sum` over the remote files. Compare with local hashes and classify:
   frontend changed, `server.py`/`storage.py`/`alerts.py`/... changed (restart `signal`), `runner.py` changed
   (restart `signal-runner`).
4. **Release dir**: back up into a single `releases/<stamp>/` holding the current `dist/`, the Python files about
   to change, and a `manifest.json` (git sha, files, hashes). This replaces the `dist-before-*` and
   `backend-before-*` scheme.
5. **Upload**:
   - stream a tar into `releases/<stamp>.incoming/`;
   - verify hashes remotely;
   - swap in: `dist` via a rename (`mv dist dist.old && mv incoming/dist dist`), then the Python files via
     `install -m 644`.
6. **Restart** only the needed units with `sudo -n systemctl restart ...`. If `sudo -n` is refused, stop and print
   the exact command instead of hanging on a password prompt.
7. **Verify**:
   - poll `curl -fsS 127.0.0.1:8091/healthz` for up to 30 s;
   - check that the served `index.html` references the new `assets/index-*.js` name;
   - if a unit was restarted, check `systemctl is-active`.
8. **Auto-rollback** on any verify failure: restore from `releases/<stamp>/`, restart again, verify again, and
   exit non-zero with a clear message.
9. **Prune**: keep the last 5 `releases/*`. With `--prune-legacy` (asks first, prints sizes), also remove the old
   `dist-before-*`, `backend-before-*` and `release-*` dirs.
10. Print a summary of what changed, what restarted and the rollback command.

Keep `update-signal.sh` as a thin wrapper that calls `deploy.py`, or delete it and update the docs. Either way
`docs/INSTALLATION.md` must describe a single path.

### Tests (`test_deploy.py`)

- Put the transport behind a small `Remote` interface. A `LocalRemote` runs the same shell snippets against a temp
  dir, so the whole flow can be tested locally.
- Cases:
  - nothing changed;
  - only dist changed (no restart);
  - `runner.py` changed (only signal-runner restarts);
  - health fails, then the release is restored and the process exits non-zero;
  - pruning keeps 5;
  - `--dry-run` writes nothing.
- systemd and curl calls are injected fakes.

### Verify on the Pi
1. `--dry-run` against the real host.
2. A frontend-only deploy.
3. `--rollback` and back.
4. `--prune-legacy` (owner confirms).

---

## 3. Semi-automatic disk cleaner

### How the space was actually investigated

The manual routine, which the tool should automate:
1. `df -h` for the overall picture, then `docker system df` (but see the containerd caveat above).
2. `du -xh --max-depth=1 /`, then drill into the biggest entry (`/var` → `/var/lib/containerd`, `/var/log`).
3. `docker images` with sizes and ages, `docker ps -a` for stopped containers, and which image each one pins.
4. `journalctl --disk-usage`, then `find /var/log -size +5M` (atop and journal).
5. apt cache, home-dir backups, and old deploy backup dirs.
6. Decide per item. Never wholesale.

### `deploy/pi-clean.py` (runs **on the Pi**, stdlib, needs sudo for some actions)

Two modes. **Scan is the default and never changes anything.**

```
sudo python3 pi-clean.py                 # scan: grouped report with sizes, safety level, proposed commands
sudo python3 pi-clean.py --json          # same, machine-readable
sudo python3 pi-clean.py apply           # interactive: walks the report, asks y/N per item (or per group)
sudo python3 pi-clean.py apply --safe    # applies only items marked "safe", still prints each first
```

The scan builds a list of **findings**. Each finding has `id`, `category`, `title`, `bytes`, `age`,
`safety` (`safe` / `review` / `report-only`), `reason`, and `action` (an argv list, never a shell string).

| Category | What | Safety | Action |
|---|---|---|---|
| docker-build-cache | builder cache | safe | `docker builder prune -f` |
| docker-dangling | `<none>` images | safe | `docker image prune -f` (dangling only) |
| docker-unused-image | one finding per image not used by any container (running **or stopped**) and not referenced by a registered app's compose (`/etc/signal/apps.json` → `docker compose config --images`); **exclude `*:signal-prev`** while the matching app exists | review | `docker image rm <id>` |
| docker-stopped | one finding per container exited > 14 days, with the image bytes it pins (e.g. ai-browser → chromium 4.5 G) and its compose project | review | `docker compose -p <project> down` if compose-managed, else `docker rm`. Its image becomes a separate `docker-unused-image` finding on the next scan |
| journal | journal above 100 M | safe | `journalctl --vacuum-size=100M` |
| journal-limit | no `SystemMaxUse` configured | review (persistent config) | write `/etc/systemd/journald.conf.d/size.conf` with `SystemMaxUse=100M`, then restart journald |
| atop | `/var/log/atop/atop_*` older than 7 days | safe | delete those files |
| atop-limit | atop keeps more than 7 days | review (persistent config) | set `LOGGENERATIONS=7` in `/etc/default/atop` |
| apt | `/var/cache/apt/archives/*.deb` | safe | `apt-get clean` |
| signal-releases | SIGNAL deploy backups beyond the newest 5 (old `*-before-*`, `release-*` and `releases/*`) | safe | delete those dirs |
| pycache | `__pycache__` under `~/apps` | safe | delete |
| home-backups | `~/backups/*` and `~/apps/*backup*` with size and age | **report-only** | none, the owner decides by hand |
| big-files | top 15 files > 100 MB not covered above (`find / -xdev`) | **report-only** | none |
| docker-volumes | dangling volumes | **report-only** | never prune volumes |

Rules:
- **Never** use `docker system prune`, `docker image prune -a` or `docker volume prune`, and never delete
  anything under a user data dir.
- Compute "reclaimable" from actual references (containers ↔ images), not from `docker system df`, which is wrong
  under the containerd snapshotter.
- Before applying, re-check each finding (the state may have changed since the scan). Skip it if it no longer
  qualifies.
- Log every applied action with the bytes freed to `/var/log/pi-clean.log`, and report the free space before and
  after.
- Config changes (journald, atop) are always their own `review` findings, never bundled into `--safe`.

### Optional phase 2: SIGNAL shows it, it does not do it

- A weekly `pi-clean.timer` (template under `deploy/`) runs `pi-clean.py --json > /var/lib/signal/cleanup.json`.
- SIGNAL reads that file and shows "~X GB could be freed (scan from <date>)" on the disk card, with the top
  findings. It shows no buttons, because applying stays a CLI step on the host.
- Only do this after phase 1 is merged and used once.

### Tests (`test_pi_clean.py`)

- Docker and system calls go through an injected runner returning canned JSON (`docker image ls --format json`,
  `docker ps -a --format json`, `docker image inspect`). Filesystem findings use a temp tree.
- Cases:
  - a `signal-prev` tag is never proposed;
  - an image used by a stopped container is not "unused" until that container goes;
  - a compose-referenced image of a registered app is kept;
  - volumes are only reported;
  - `apply --safe` runs safe items only;
  - re-check skips stale findings;
  - nothing runs in scan mode, asserted on the fake runner.

### Verify on the Pi
1. Run the scan and compare its totals with the table at the top of this doc.
2. Owner runs `apply` and picks items; expected big wins are the ai-browser chromium image (4.5 G), atop logs and
   the SIGNAL leftovers.

---

## Order and parallelism

The three workstreams touch separate files, so they can run in parallel:
- health: `health.py`, `server.py`, `alerts.py`, `storage.py`, `src/`;
- deploy: `deploy/deploy.py`, docs;
- cleaner: `deploy/pi-clean.py`.

Deploy is worth merging first, because the other two ship through it. Cleaner phase 2 waits for health only if both
touch the disk card; coordinate on `src/main.tsx` then.
