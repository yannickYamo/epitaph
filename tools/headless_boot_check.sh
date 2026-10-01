#!/usr/bin/env bash
# The headless boot test (BUILD_PLAN 4, 10.4 "Headless boot", card E4; gate G1.3).
#
#   tools/headless_boot_check.sh [options]
#     --reboot       reboot the Pi first and wait for it (without it: check the current boot)
#     --timeout S    seconds to wait for SSH after the reboot (default 420)
#     --screen       a screen is connected: the display unit must run instead of being skipped
#     --out FILE     also write the summary to FILE
#     --agent A      name on the Pi lock (default $AGENT, else E)
#     --dry-run      print the commands; no lock, no SSH, no reboot
#
# Checks, on the boot that is current when it runs (after --reboot, the new one):
#   display     epitaph-display.service is loaded and enabled, was skipped by its ExecCondition
#               (Result=exec-condition, or ConditionResult=no for a Condition*= line), is not
#               failed or restarting, and NRestarts=0: no crash loop with no screen
#   controller  epitaph-controller.service is enabled and active (NRestarts is reported)
# and reports, without judging: boot time, seconds from the reboot to SSH, the watchdog,
# vcgencmd get_throttled. Exit 0 every check passes; 1 a check fails or the Pi does not come
# back; 2 usage.
#
# Environment: PI_HOST (skip the alias probe), EPITAPH_SSH (default ssh).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARGS=("$@")
REBOOT=0
TIMEOUT_S=420
SCREEN=0
OUT=""
AGENT="${AGENT:-E}"
DRY=0
usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; }
while [ $# -gt 0 ]; do
  case "$1" in
    --reboot) REBOOT=1; shift ;;
    --timeout) TIMEOUT_S="${2:?--timeout needs a value}"; shift 2 ;;
    --screen) SCREEN=1; shift ;;
    --out) OUT="${2:?--out needs a value}"; shift 2 ;;
    --agent) AGENT="${2:?--agent needs a value}"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "headless_boot_check: unknown option $1" >&2; usage >&2; exit 2 ;;
  esac
done
[[ "$TIMEOUT_S" =~ ^[1-9][0-9]*$ ]] || { echo "headless_boot_check: --timeout must be seconds" >&2; exit 2; }
SSH="${EPITAPH_SSH:-ssh}"
log() { printf '[boot_check %s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
exec 3>&2  # dry-run lines go here, past any 2>/dev/null on the command

if [ "$DRY" = 0 ] && [ "${EPITAPH_PI_LOCKED:-}" != 1 ]; then
  minutes=$(( TIMEOUT_S / 60 + 10 ))
  log "taking the Pi lock for ${minutes} min as $AGENT"
  exec "$ROOT/tools/pi_lock.sh" run "$AGENT" "$minutes" -- env EPITAPH_PI_LOCKED=1 "$0" "${ARGS[@]}"
fi

# shellcheck source=tools/pi_host.sh
. "$ROOT/tools/pi_host.sh"
find_host() { if [ "$DRY" = 1 ]; then printf '%s\n' "${PI_HOST:-pi}"; else pi_host 2>/dev/null; fi; }
quote() { local a out=""; for a in "$@"; do out+="$(printf '%q' "$a") "; done; printf '%s' "${out% }"; }
remote() {
  if [ "$DRY" = 1 ]; then printf '+ %s %s %s\n' "$SSH" "$HOST" "$(quote "$@")" >&3; return 0; fi
  "$SSH" -o BatchMode=yes -o ConnectTimeout=10 "$HOST" "$(quote "$@")"
}

HOST="$(find_host)" || { log "FAIL: the Pi does not answer (tools/pi_host.sh)"; exit 1; }

# --- the reboot ------------------------------------------------------------------------------
UP_S=""
if [ "$REBOOT" = 1 ]; then
  OLD_BOOT="$(remote cat /proc/sys/kernel/random/boot_id)" \
    || { log "FAIL: the Pi does not answer on $HOST (no boot id before the reboot)"; exit 1; }
  log "rebooting the Pi (boot ${OLD_BOOT:-?}) through $HOST"
  # The connection drops as the Pi goes down: exit 255 is the expected answer.
  remote sudo -n systemctl reboot || true
  start_s=$SECONDS
  NEW_BOOT=""
  while [ "$DRY" = 0 ]; do
    if [ $((SECONDS - start_s)) -gt "$TIMEOUT_S" ]; then
      log "FAIL: the Pi did not answer within ${TIMEOUT_S}s of the reboot"; exit 1
    fi
    sleep 10
    HOST="$(find_host)" || continue
    NEW_BOOT="$(remote cat /proc/sys/kernel/random/boot_id 2>/dev/null)" || continue
    [ -n "$NEW_BOOT" ] && [ "$NEW_BOOT" != "$OLD_BOOT" ] && break
  done
  UP_S=$((SECONDS - start_s))
  log "back on $HOST after ${UP_S}s (boot ${NEW_BOOT:-?}); waiting for the boot to settle"
  # `--wait` returns once start-up is over (running or degraded); the display unit's
  # ExecCondition has run by then.
  remote timeout 180 systemctl is-system-running --wait > /dev/null || true
fi

# --- the facts ------------------------------------------------------------------------------
PROPS=(LoadState UnitFileState ActiveState SubState Result NRestarts ConditionResult ExecMainStatus)
show() { remote systemctl show "$1" "${PROPS[@]/#/--property=}" 2>/dev/null || true; }
DISPLAY_SHOW="$(show epitaph-display)"
CONTROLLER_SHOW="$(show epitaph-controller)"
SYSTEM="$(remote systemctl is-system-running 2>/dev/null || true)"
BOOT_TIME="$(remote systemd-analyze time 2>/dev/null | head -n 1 || true)"
WATCHDOG="$(remote systemctl show --property=RuntimeWatchdogUSec --value 2>/dev/null || true)"
THROTTLED="$(remote vcgencmd get_throttled 2>/dev/null || true)"
DISPLAY_LOG="$(remote journalctl -b --no-pager -o cat -n 5 -u epitaph-display 2>/dev/null || true)"

prop() { printf '%s\n' "$1" | sed -n "s/^$2=//p" | head -n 1; }

FAIL=0
LINES=()
judge() {  # judge NAME OK DETAIL
  local status=PASS
  [ "$2" = 1 ] || { status=FAIL; FAIL=1; }
  LINES+=("$(printf '  %-4s %-20s %s' "$status" "$1" "$3")")
}

if [ "$DRY" = 1 ]; then
  log "dry run: nothing was checked"
  exit 0
fi

d_load="$(prop "$DISPLAY_SHOW" LoadState)"; d_file="$(prop "$DISPLAY_SHOW" UnitFileState)"
d_active="$(prop "$DISPLAY_SHOW" ActiveState)"; d_sub="$(prop "$DISPLAY_SHOW" SubState)"
d_result="$(prop "$DISPLAY_SHOW" Result)"; d_restarts="$(prop "$DISPLAY_SHOW" NRestarts)"
d_cond="$(prop "$DISPLAY_SHOW" ConditionResult)"
c_load="$(prop "$CONTROLLER_SHOW" LoadState)"; c_file="$(prop "$CONTROLLER_SHOW" UnitFileState)"
c_active="$(prop "$CONTROLLER_SHOW" ActiveState)"; c_restarts="$(prop "$CONTROLLER_SHOW" NRestarts)"

judge display-installed "$([ "$d_load" = loaded ] && [ "$d_file" = enabled ] && echo 1)" \
  "LoadState=${d_load:-?} UnitFileState=${d_file:-?} (a unit that is not enabled proves nothing)"
judge display-restarts "$([ "${d_restarts:-x}" = 0 ] && echo 1)" "NRestarts=${d_restarts:-?}"
judge display-not-failing "$(case "$d_active" in failed|activating|deactivating|'') ;; *) echo 1 ;; esac)" \
  "ActiveState=${d_active:-?} SubState=${d_sub:-?}"
if [ "$SCREEN" = 1 ]; then
  judge display-runs "$([ "$d_active" = active ] && echo 1)" "a screen is connected: ActiveState=${d_active:-?}"
else
  judge display-skipped "$([ "$d_active" != active ] && { [ "$d_result" = exec-condition ] || [ "$d_cond" = no ]; } && echo 1)" \
    "Result=${d_result:-?} ConditionResult=${d_cond:-?} (no screen: ExecCondition skips the unit)"
fi
judge controller-installed "$([ "$c_load" = loaded ] && [ "$c_file" = enabled ] && echo 1)" \
  "LoadState=${c_load:-?} UnitFileState=${c_file:-?}"
judge controller-active "$([ "$c_active" = active ] && echo 1)" \
  "ActiveState=${c_active:-?} NRestarts=${c_restarts:-?}"

SUMMARY="$(
  printf 'headless boot check on %s, %s: %s\n' "$HOST" "$(date -Is)" "$([ "$FAIL" = 0 ] && echo PASS || echo FAIL)"
  printf '%s\n' "${LINES[@]}"
  printf '  info reboot-to-ssh       %s\n' "${UP_S:-no reboot (--reboot not given)}"
  printf '  info system              %s\n' "${SYSTEM:-?}"
  printf '  info boot time           %s\n' "${BOOT_TIME:-?}"
  printf '  info watchdog            RuntimeWatchdogUSec=%s\n' "${WATCHDOG:-?}"
  printf '  info power               %s\n' "${THROTTLED:-?}"
  printf '  info display journal     %s\n' "$(printf '%s' "$DISPLAY_LOG" | tr '\n' '|')"
)"
printf '%s\n' "$SUMMARY"
if [ -n "$OUT" ]; then mkdir -p "$(dirname "$OUT")"; printf '%s\n' "$SUMMARY" > "$OUT"; fi
exit "$FAIL"
