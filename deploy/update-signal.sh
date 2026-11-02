#!/bin/sh
# Kept for muscle memory: deploys SIGNAL with deploy/deploy.py (see docs/INSTALLATION.md).
#   deploy/update-signal.sh alexpi@100.105.170.124 [--dry-run | --rollback | --list ...]
set -eu
here=$(dirname "$0")
if command -v python3 >/dev/null 2>&1; then exec python3 "$here/deploy.py" "$@"; fi
exec python "$here/deploy.py" "$@"
