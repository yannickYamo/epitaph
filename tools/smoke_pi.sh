#!/usr/bin/env bash
# Run lives on the Pi and judge them on the laptop (9 E4; gates G1.1, G1.4).
#
#   tools/smoke_pi.sh [options]
#     --profile P    profile to run (default pi4/smoke-300)
#     --lives N      consecutive lives (default 1; with 2 the first one's next_birth is judged)
#     --level L      verify-life level (default: smoke for pi4/smoke-300, else the profile's own)
#     --hardware H   overlay verify-life judges by (default pi4-4gb)
#     --out DIR      where the lives are copied (default logs/pi/<stamp>-<profile>)
#     --holder NAME   name on the Pi lock (default $HOLDER, else E)
#     --dry-run      print the commands; no lock, no SSH, no verify
#
# It deploys nothing (`make pi-deploy` does). In one hold of the Pi lock it:
#   1. finds the Pi (tools/pi_host.sh), the installed `epitaph`, the controller's user and the
#      newest life folder, and refuses if an earlier run's unit or a llama job is still running;
#   2. stops epitaph-controller.service if it is active (two controllers refuse to run) and
#      starts it again at the end, whatever happens;
#   3. runs `epitaph run --profile P --lives N` as a detached unit (`epitaph-pilife-<stamp>`,
#      PI_LOCK "Long jobs"), so a dropped SSH session cannot kill the life, and polls it;
#   4. copies the new lives/NNNNNN folders and the unit's journal to --out;
#   5. runs `epitaph verify-life` on each life (its next life feeds next_birth) and writes
#      verify.json next to each events.jsonl.
# Exit: 0 every life passes; 1 a life fails, is missing, or the run failed; 2 usage.
#
# Environment: PI_HOST (skip the alias probe), EPITAPH_PI_BIN (the epitaph command on the Pi;
# default: on PATH, else /opt/epitaph/venv/bin/epitaph (from deploy/install.sh), else
# ~/epitaph/.venv/bin/epitaph),
# EPITAPH_PI_STATE (default /var/lib/epitaph), EPITAPH_PI_TMP (default /var/tmp),
# EPITAPH_RUN_ARGS (extra `epitaph run` arguments), EPITAPH_PYTHON (laptop Python, default
# .venv/bin/python), EPITAPH_SSH (default ssh), EPITAPH_POLL_S (default 15).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARGS=("$@")
PROFILE=pi4/smoke-300
LIVES=1
LEVEL=""
HARDWARE=pi4-4gb
OUT=""
HOLDER="${HOLDER:-make}"
DRY=0
usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; }
while [ $# -gt 0 ]; do
  case "$1" in
    --profile) PROFILE="${2:?--profile needs a value}"; shift 2 ;;
    --lives) LIVES="${2:?--lives needs a value}"; shift 2 ;;
    --level) LEVEL="${2:?--level needs a value}"; shift 2 ;;
    --hardware) HARDWARE="${2:?--hardware needs a value}"; shift 2 ;;
    --out) OUT="${2:?--out needs a value}"; shift 2 ;;
    --holder) HOLDER="${2:?--holder needs a value}"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "smoke_pi: unknown option $1" >&2; usage >&2; exit 2 ;;
  esac
done
[[ "$LIVES" =~ ^[1-9][0-9]?$ ]] || { echo "smoke_pi: --lives must be 1-99" >&2; exit 2; }
[[ "$PROFILE" =~ ^[A-Za-z0-9_./-]+$ ]] || { echo "smoke_pi: bad profile name" >&2; exit 2; }
[ -n "$LEVEL" ] || { [ "$PROFILE" = pi4/smoke-300 ] && LEVEL=smoke; }

PYTHON="${EPITAPH_PYTHON:-$ROOT/.venv/bin/python}"
SSH="${EPITAPH_SSH:-ssh}"
POLL_S="${EPITAPH_POLL_S:-15}"
STATE="${EPITAPH_PI_STATE:-/var/lib/epitaph}"
TMPD="${EPITAPH_PI_TMP:-/var/tmp}"
pyrun() { PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON" "$@"; }
log() { printf '[smoke_pi %s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
exec 3>&2  # dry-run lines go here, past any 2>/dev/null on the command

# The time budget: each life's lifespan, a load (load_timeout_s), the death screen and the
# silence, plus a margin. The lock is held for the budget plus the copy and the checks.
read -r LIFESPAN_S SILENCE_S LOAD_S < <(pyrun - "$PROFILE" "$HARDWARE" <<'PY'
import sys
from epitaph.config import load_config
cfg = load_config(sys.argv[1], sys.argv[2], validate=False)
print(int(cfg.profile.lifespan_s), int(cfg.get("life.silence_seconds", 90)),
      int(cfg.get("life.load_timeout_s", 300)))
PY
) || { echo "smoke_pi: cannot read profile $PROFILE" >&2; exit 2; }
BUDGET_S=$(( LIVES * (LIFESPAN_S + LOAD_S + 180) + (LIVES - 1) * SILENCE_S + 300 ))
LOCK_MIN=$(( BUDGET_S / 60 + 15 ))

STAMP="$(date +%Y%m%d-%H%M%S)"
UNIT="epitaph-pilife-$STAMP"
[ -n "$OUT" ] || OUT="$ROOT/logs/pi/$STAMP-${PROFILE//\//-}"

if [ "$DRY" = 0 ] && [ "${EPITAPH_PI_LOCKED:-}" != 1 ]; then
  log "taking the Pi lock for ${LOCK_MIN} min as $HOLDER"
  exec "$ROOT/tools/pi_lock.sh" run "$HOLDER" "$LOCK_MIN" -- \
    env EPITAPH_PI_LOCKED=1 "$0" "${ARGS[@]}" --out "$OUT"
fi

if [ "$DRY" = 1 ]; then
  HOST="${PI_HOST:-pi}"
else
  # shellcheck source=tools/pi_host.sh
  . "$ROOT/tools/pi_host.sh"
  HOST="$(pi_host)" || exit 1
fi

# SSH joins its arguments into one line for the remote shell, so each is quoted here.
quote() { local a out=""; for a in "$@"; do out+="$(printf '%q' "$a") "; done; printf '%s' "${out% }"; }
# remote CMD...: one SSH command; in a dry run, print it and answer nothing.
remote() {
  if [ "$DRY" = 1 ]; then printf '+ %s %s %s\n' "$SSH" "$HOST" "$(quote "$@")" >&3; return 0; fi
  "$SSH" -o BatchMode=yes -o ConnectTimeout=10 "$HOST" "$(quote "$@")"
}
# remote_script NAME ARGS... < script: run a bash script on the Pi with arguments.
remote_script() {
  local name="$1"; shift
  if [ "$DRY" = 1 ]; then
    printf '+ %s %s bash -s -- %s   # %s:\n' "$SSH" "$HOST" "$(quote "$@")" "$name" >&3
    sed "s/^/    /" >&3
    return 0
  fi
  "$SSH" -o BatchMode=yes -o ConnectTimeout=10 "$HOST" "bash -s -- $(quote "$@")"
}

log "profile $PROFILE, $LIVES life(s), level ${LEVEL:-from the profile}, host $HOST, budget ${BUDGET_S}s"
log "lives go to $OUT"

# --- 1. what is on the Pi ----------------------------------------------------------------
FACTS="$(remote_script discover "$STATE" "${EPITAPH_PI_BIN:-}" <<'SH'
state="$1"; epi="$2"
if [ -z "$epi" ]; then
  epi="$(command -v epitaph || true)"
  for c in /opt/epitaph/venv/bin/epitaph "$HOME/epitaph/.venv/bin/epitaph"; do
    [ -n "$epi" ] || { [ -x "$c" ] && epi="$c"; }
  done
fi
echo "epitaph=$epi"
echo "controller=$(systemctl is-active epitaph-controller 2>/dev/null || true)"
echo "user=$(systemctl show -p User --value epitaph-controller 2>/dev/null || true)"
echo "busy=$(systemctl list-units --plain --no-legend --state=active,activating 'epitaph-pilife-*' 'llama-*' 2>/dev/null | awk '{print $1}' | tr '\n' ' ')"
echo "last=$(ls "$state/lives" 2>/dev/null | grep -E '^[0-9]+$' | sort | tail -n 1)"
SH
)"
fact() { printf '%s\n' "$FACTS" | sed -n "s/^$1=//p" | head -n 1; }
EPI="$(fact epitaph)"; CTRL="$(fact controller)"; RUN_USER="$(fact user)"
BUSY="$(fact busy)"; LAST="$(fact last)"
if [ "$DRY" = 1 ]; then EPI="${EPITAPH_PI_BIN:-epitaph}"; CTRL=active; LAST=000000; fi
RUN_USER="${RUN_USER:-pi}"
LAST="${LAST:-000000}"
[ -n "$EPI" ] || { log "FAIL: no epitaph command on the Pi (run make pi-deploy, or set EPITAPH_PI_BIN)"; exit 1; }
[ -z "${BUSY// /}" ] || { log "FAIL: still running on the Pi: $BUSY (stop or wait for it; PI_LOCK 'Long jobs')"; exit 1; }
log "epitaph: $EPI; controller: ${CTRL:-not installed}; runs as $RUN_USER; newest life before: $LAST"

# --- 2. the installed controller steps aside -----------------------------------------------
RESTART=0
restart_controller() {
  if [ "$RESTART" = 1 ]; then
    RESTART=0
    log "starting epitaph-controller again"
    remote sudo -n systemctl start epitaph-controller || log "WARNING: epitaph-controller did not start"
  fi
}
trap restart_controller EXIT
trap 'log "interrupted; stopping $UNIT"; remote sudo -n systemctl stop "$UNIT" || true; exit 1' INT TERM
if [ "$CTRL" = active ]; then
  log "stopping epitaph-controller for the run"
  remote sudo -n systemctl stop epitaph-controller
  RESTART=1
fi

# --- 3. the lives, as a detached unit --------------------------------------------------------
RC_FILE="$TMPD/$UNIT.rc"
# shellcheck disable=SC2086  # EPITAPH_RUN_ARGS is a word list on purpose
if remote_script start "$UNIT" "$RUN_USER" "$RC_FILE" "$EPI" run --profile "$PROFILE" \
  --lives "$LIVES" ${EPITAPH_RUN_ARGS:-} <<'SH'
unit="$1"; user="$2"; rc_file="$3"; shift 3
group="$(id -gn "$user")"; home="$(getent passwd "$user" | cut -d: -f6)"
rm -f "$rc_file"
# As the installed unit runs it: a delegated cgroup for the creature, the
# controller on core 0. The exit code goes to a file: a finished transient unit is unloaded,
# and with it its result.
sudo -n systemd-run --unit="$unit" --uid="$user" --gid="$group" --setenv=HOME="$home" \
  --property=Delegate=yes --property=CPUAffinity=0 --property=KillMode=control-group --collect \
  /bin/sh -c '"$@"; echo $? > "$0"' "$rc_file" "$@"
SH
then
  log "started $UNIT"
else
  log "FAIL: could not start $UNIT (passwordless sudo and systemd-run on the Pi?)"; exit 1
fi

start_s=$SECONDS
ssh_fail=0
while [ "$DRY" = 0 ]; do
  set +e
  state="$(remote systemctl is-active "$UNIT" 2>/dev/null)"; rc=$?
  set -e
  if [ "$rc" = 255 ]; then
    ssh_fail=$((ssh_fail + 1))
    [ "$ssh_fail" -le 40 ] || { log "FAIL: the Pi stopped answering"; exit 1; }
  else
    ssh_fail=0
    case "$state" in active|activating|deactivating|reloading) ;; *) break ;; esac
  fi
  if [ $((SECONDS - start_s)) -gt "$BUDGET_S" ]; then
    log "FAIL: $UNIT still running after ${BUDGET_S}s; stopping it"
    remote sudo -n systemctl stop "$UNIT" || true
    break
  fi
  sleep "$POLL_S"
done
log "the run ended after $((SECONDS - start_s))s"

JOURNAL=/dev/null
if [ "$DRY" = 0 ]; then mkdir -p "$OUT/lives"; JOURNAL="$OUT/journal.txt"; fi
RUN_RC="$(remote cat "$RC_FILE" 2>/dev/null || true)"
remote journalctl --no-pager -o short-iso -u "$UNIT" > "$JOURNAL" || true
[ "$DRY" = 0 ] || RUN_RC=0
restart_controller

# --- 4. copy the new lives back ------------------------------------------------------------
NEW="$(remote_script list "$STATE" "$LAST" <<'SH'
ls "$1/lives" 2>/dev/null | grep -E '^[0-9]+$' | sort | awk -v last="$2" '$0 > last'
SH
)"
if [ "$DRY" = 1 ]; then NEW="$(for i in $(seq 1 "$LIVES"); do printf '%06d\n' "$i"; done)"; fi
mapfile -t NEW_LIVES < <(printf '%s\n' "$NEW" | grep -E '^[0-9]+$' || true)
if [ "${#NEW_LIVES[@]}" -gt 0 ]; then
  if [ "$DRY" = 1 ]; then
    printf '+ %s %s tar -C %s/lives -cf - %s | tar -C %s/lives -xf -\n' \
      "$SSH" "$HOST" "$STATE" "${NEW_LIVES[*]}" "$OUT" >&2
  else
    remote tar -C "$STATE/lives" -cf - "${NEW_LIVES[@]}" | tar -C "$OUT/lives" -xf -
  fi
fi
log "copied ${#NEW_LIVES[@]} life folder(s): ${NEW_LIVES[*]:-none}; unit exit ${RUN_RC:-unknown}"

# --- 5. judge them -------------------------------------------------------------------------
FAIL=0
[ "${RUN_RC:-x}" = 0 ] || { log "FAIL: epitaph run exited ${RUN_RC:-without a code} (see $OUT/journal.txt)"; FAIL=1; }
[ "${#NEW_LIVES[@]}" -ge "$LIVES" ] || { log "FAIL: $LIVES life(s) asked for, ${#NEW_LIVES[@]} recorded"; FAIL=1; }
i=0
for n in "${NEW_LIVES[@]}"; do
  i=$((i + 1))
  [ "$i" -le "$LIVES" ] || break  # a life born after the last one asked for is only context
  cmd=(-m epitaph verify-life "$OUT/lives/$n" --profile "$PROFILE" --hardware "$HARDWARE")
  [ -z "$LEVEL" ] || cmd+=(--level "$LEVEL")
  if [ "$DRY" = 1 ]; then
    printf '+ PYTHONPATH=%s/src %s %s\n' "$ROOT" "$PYTHON" "${cmd[*]}" >&2
    continue
  fi
  set +e
  pyrun "${cmd[@]}"; rc=$?
  set -e
  [ "$rc" = 0 ] || FAIL=1
done

if [ "$DRY" = 1 ]; then
  log "dry run: nothing ran"
elif [ "$FAIL" = 0 ]; then
  log "PASS: ${#NEW_LIVES[@]} life(s) of $PROFILE; evidence in $OUT (verify.json per life)"
else
  log "FAIL: see the lines above; evidence in $OUT"
fi
exit "$FAIL"
