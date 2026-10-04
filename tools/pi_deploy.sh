#!/usr/bin/env bash
# Deploy this working tree to the Pi and install it (BUILD_PLAN 8.3, 9 C5).
#
#   tools/pi_lock.sh run <name> 15 -- tools/pi_deploy.sh [install.sh options]
#   make pi-deploy                     (the same)
#
# 1. rsync the working tree to <host>:/opt/epitaph/src (no .git, venvs, voice/, caches, nor
#    anything .gitignore excludes; files deleted here are deleted there)
# 2. run deploy/install.sh there as root (idempotent; options such as --enable, --check,
#    --no-selftest pass through)
#
# The host is PI_HOST (environment or .pi.env), else `pi` with the cable as the fallback
# (tools/pi_host.sh). It must run under the Pi lock: it refuses when the lock is not held by
# one of its own ancestors.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST=/opt/epitaph/src

# --- the Pi lock must be held by our caller --------------------------------------------------
lock_held_by_ancestor() {
  local owner pid p
  owner="${EPITAPH_LOCK_DIR:-$HOME/.local/state/epitaph/locks}/pi.owner"
  [ -r "$owner" ] || return 1
  pid="$(sed -n 's/.* pid=\([0-9]*\) .*/\1/p' "$owner")"
  [ -n "$pid" ] || return 1
  p=$$
  while [ -n "$p" ] && [ "$p" -gt 1 ]; do
    [ "$p" = "$pid" ] && return 0
    p="$(awk '{print $4}' "/proc/$p/stat" 2>/dev/null || true)"
  done
  return 1
}
if ! lock_held_by_ancestor; then
  echo "pi_deploy.sh must run under the Pi lock: tools/pi_lock.sh run <agent> 15 -- $0" >&2
  exit 2
fi

# --- the host --------------------------------------------------------------------------------
if [ -z "${PI_HOST:-}" ] && [ -r "$ROOT/.pi.env" ]; then
  PI_HOST="$(sed -n 's/^PI_HOST=//p' "$ROOT/.pi.env" | head -n1)"
fi
# shellcheck source=tools/pi_host.sh
. "$ROOT/tools/pi_host.sh"
HOST="$(pi_host)" || exit 3
ssh_() { ssh -o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=15 "$HOST" "$@"; }
echo "== deploying $ROOT to $HOST:$DEST"

# --- 1. the source -----------------------------------------------------------------------------
# /opt/epitaph belongs to the service user (install.sh keeps it so); create it on first deploy.
ssh_ "test -w $DEST || sudo -n install -d -o \"\$(id -un)\" -g \"\$(id -gn)\" -m 0755 /opt/epitaph $DEST"
rsync -rlptz --delete --itemize-changes \
  --exclude=/.git --exclude=/.claude/ --exclude=/.venv --exclude=/venv/ --exclude=/voice/ \
  --exclude=__pycache__/ --exclude=.pytest_cache/ --exclude=.ruff_cache/ --exclude=.mypy_cache/ \
  --exclude='*.egg-info/' --exclude=/.pi.env --exclude=.coverage --exclude=/htmlcov/ \
  --filter=':- .gitignore' \
  -e "ssh -o BatchMode=yes -o ConnectTimeout=15" \
  "$ROOT/" "$HOST:$DEST/" | sed 's/^/  /'

# --- 2. install ------------------------------------------------------------------------------
quoted=""
for a in "$@"; do quoted+=" $(printf '%q' "$a")"; done
ssh_ "sudo -n $DEST/deploy/install.sh$quoted"
