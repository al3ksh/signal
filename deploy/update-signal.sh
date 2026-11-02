#!/bin/sh
# Deploy SIGNAL itself to a native (systemd) install. Run from a checkout on your
# workstation or in CI; it never builds on the Pi.
#
#   deploy/update-signal.sh alexpi@100.105.170.124
#
# 1. Builds the frontend locally (skip with SKIP_BUILD=1 when dist/ is already built).
# 2. Copies the current remote dist/ to dist-before-<timestamp> (keeps the last 5).
# 3. Syncs dist/ and, only when they differ, the Python files.
# 4. Restarts signal.service only when server-side Python changed, and
#    signal-runner.service only when runner.py changed (needs sudo on the host).
# The runner does not manage SIGNAL; this script is the supported update path.
set -eu

HOST=${1:-${SIGNAL_DEPLOY_HOST:-}}
DIR=${SIGNAL_DEPLOY_DIR:-/home/alexpi/apps/signal}
PYTHON_FILES="server.py storage.py alerts.py runner.py setup_access.py"

if [ -z "$HOST" ]; then
  echo "usage: $0 user@host   (or set SIGNAL_DEPLOY_HOST; SIGNAL_DEPLOY_DIR defaults to $DIR)" >&2
  exit 2
fi
cd "$(dirname "$0")/.."

if [ "${SKIP_BUILD:-0}" != 1 ]; then
  npm ci
  npm run build
fi
[ -f dist/index.html ] || { echo "dist/index.html is missing; build first." >&2; exit 1; }

stamp=$(date +%Y%m%d-%H%M%S)
# Which Python files differ? (dry run, compared by checksum)
changed=$(rsync -rci --dry-run $PYTHON_FILES "$HOST:$DIR/" | awk '/^<f/ {print $2}')

echo "Backing up the deployed frontend to dist-before-$stamp"
ssh "$HOST" "cd '$DIR' && if [ -d dist ]; then cp -a dist 'dist-before-$stamp'; fi && ls -1d dist-before-* 2>/dev/null | sort | head -n -5 | xargs -r rm -rf"

echo "Syncing dist/"
rsync -a --delete dist/ "$HOST:$DIR/dist/"

if [ -z "$changed" ]; then
  echo "Python unchanged; no restart needed. Reload the page to get the new frontend."
  exit 0
fi

echo "Python changed: $changed"
rsync -ac $PYTHON_FILES "$HOST:$DIR/"
restart=""
for file in $changed; do
  case "$file" in
    runner.py) restart="$restart signal-runner.service" ;;
    setup_access.py) ;;
    *) case "$restart" in *" signal.service"*) ;; *) restart="$restart signal.service" ;; esac ;;
  esac
done
if [ -n "$restart" ]; then
  echo "Restarting:$restart"
  # shellcheck disable=SC2086
  ssh -t "$HOST" "sudo systemctl restart$restart && systemctl --no-pager --lines=0 status$restart"
fi
echo "Rollback: ssh $HOST \"cd '$DIR' && rm -rf dist && mv dist-before-$stamp dist\""
