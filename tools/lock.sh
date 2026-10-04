#!/usr/bin/env bash
# Named exclusive locks shared by every job on this laptop (BUILD_PLAN 8.3).
# All jobs start on the laptop, so a local flock serialises them. The kernel releases
# the lock when the holder exits, so a crashed job can never leave a stale lock, and
# the time limit kills a command that overruns its reservation.
#
#   lock.sh <name> run <holder> <minutes> -- <command...>   wait for the lock, run, release
#   lock.sh <name> status                                   who holds it, since when
set -euo pipefail

NAME="${1:?lock name}"; shift
DIR="${EPITAPH_LOCK_DIR:-$HOME/.local/state/epitaph/locks}"
mkdir -p "$DIR"
LOCK="$DIR/$NAME.lock"
OWNER="$DIR/$NAME.owner"
WAIT_S="${EPITAPH_LOCK_WAIT_S:-14400}"

case "${1:-}" in
  run)
    holder="${2:?holder}"; minutes="${3:?minutes}"; shift 3
    [[ "$minutes" =~ ^[1-9][0-9]*$ ]] || { echo "minutes must be a positive integer" >&2; exit 2; }
    [ "${1:-}" = "--" ] && shift
    [ "$#" -gt 0 ] || { echo "no command given" >&2; exit 2; }
    exec 9>"$LOCK"
    if ! flock -n 9; then
      echo "[$NAME lock] held by: $(cat "$OWNER" 2>/dev/null || echo unknown); waiting up to ${WAIT_S}s" >&2
      flock -w "$WAIT_S" 9 || { echo "[$NAME lock] gave up waiting" >&2; exit 75; }
    fi
    printf '%s pid=%s since=%s max=%smin cmd=%s\n' "$holder" "$$" "$(date -Is)" "$minutes" "$*" > "$OWNER"
    trap 'rm -f "$OWNER"' EXIT
    set +e
    timeout --kill-after=30 "$((minutes * 60))" "$@"
    rc=$?
    [ "$rc" -eq 124 ] && echo "[$NAME lock] command exceeded ${minutes} min and was stopped" >&2
    exit "$rc"
    ;;
  status)
    if flock -n "$LOCK" true 2>/dev/null; then echo "$NAME: free"; else echo "$NAME: held by $(cat "$OWNER" 2>/dev/null || echo unknown)"; fi
    ;;
  *)
    echo "usage: lock.sh <name> run <holder> <minutes> -- <cmd...> | lock.sh <name> status" >&2; exit 2 ;;
esac
