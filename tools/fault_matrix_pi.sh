#!/usr/bin/env bash
# The fault matrix on the Pi (9 E6; gates G2.2, A3).
#
#   tools/fault_matrix_pi.sh [options]
#     --rows a,b     only these rows (names from --list)
#     --list         print every row and how it runs; touches nothing
#     --out FILE     the table (default logs/pi/faults-<stamp>.md); each row's full output goes
#                    to the folder of the same name without .md
#     --holder NAME   name on the Pi lock (default $HOLDER, else E)
#     --row-min M    time limit per row in minutes (default 40)
#     --dry-run      print the plan; no lock, no SSH, no fault
#
# Rows run one at a time, in one hold of the Pi lock:
#   pi      through tools/fault_pi.sh <row> (one row per call; it prints a line that
#           starts with PASS or FAIL and exits 0 or 1). The driver holds the lock and sets
#           EPITAPH_PI_LOCKED=1 (the smoke_pi.sh convention), so fault_pi.sh must not take it
#           again; one that tries gives up at once (exit 75) and the row is an error. Exit 2
#           means fault_pi.sh does not know the row: `not run`.
#   native  run by this driver: `headless-boot` is tools/headless_boot_check.sh on the current
#           boot (no reboot), `two-holders` proves a second lock request queues behind this one.
#   owner   needs a person or a fresh SD image (power cut, cable, laptop, reboots): listed as
#           `owner` and never run here.
# Before the first row it notes whether epitaph-controller is active; at the end, even after
# a failure or a timeout, it starts the controller again if it was active and no longer is,
# and records the controller, the CPU clock cap and any leftover test units in the table.
# Exit: 0 when every row that ran passed and none was `not run`; 1 otherwise; 2 usage.
#
# Environment: FAULT_PI (default tools/fault_pi.sh), PI_HOST (skip the alias probe),
# EPITAPH_SSH (default ssh).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARGS=("$@")
ONLY=""
LIST=0
OUT=""
HOLDER="${HOLDER:-make}"
ROW_MIN=40
DRY=0
usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; }
while [ $# -gt 0 ]; do
  case "$1" in
    --rows) ONLY="${2:?--rows needs a value}"; shift 2 ;;
    --list) LIST=1; shift ;;
    --out) OUT="${2:?--out needs a value}"; shift 2 ;;
    --holder) HOLDER="${2:?--holder needs a value}"; shift 2 ;;
    --row-min) ROW_MIN="${2:?--row-min needs a value}"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "fault_matrix_pi: unknown option $1" >&2; usage >&2; exit 2 ;;
  esac
done
[[ "$ROW_MIN" =~ ^[1-9][0-9]*$ ]] || { echo "fault_matrix_pi: --row-min must be minutes" >&2; exit 2; }

# name|how|fault|expected
ROWS=(
  "ram-death|pi|RAM death (\`death_mode = oom\`)|\`cause=oom\` within 10 s; next life after the silence"
  "delegated-cgroups|pi|Delegated cgroups|every S3b step passes (\`epitaph selftest\` under the service)"
  "creature-network|pi|Creature network blocked|outbound connection from the creature cgroup refused"
  "crash|pi|Crash|\`kill -9\` on the creature: \`cause=crash\`; next life"
  "hang|pi|Hang|\`kill -STOP\`: \`cause=hang\` after the timeout; cgroup killed"
  "slow-first-token|pi|Slow first token at low CPU share|CPU share 0.7, 1000-token prompt: no false \`hang\`"
  "sd-card-wait|pi|Waiting on the SD card|\`memory.high\` probe 60 s: no false \`hang\`"
  "full-context|pi|Full context (\`unbounded\`)|small \`ctx\`: \`cause=full\`"
  "reload-longer-than-gap|pi|Reload longer than a keyframe gap|cold reload: \`reload_skipped\`, current target loaded"
  "deadline-during-reload|pi|Deadline during a reload|short lifespan: \`cause=deadline\`, nothing left running"
  "controller-killed|pi|Controller killed|restarted; previous life \`interrupted\`; no creature left; counter + 1"
  "controller-stops-pinging|pi|Controller stops pinging|systemd restarts it"
  "display-killed|pi|Display or remote view killed|life continues; redraw from a snapshot within 5 s"
  "slow-subscriber|pi|Slow subscriber|controller timing unchanged; snapshot after overflow"
  "two-controllers|pi|Two controllers|refuses; points to \`epitaph ctl new-life\`"
  "headless-boot|native|Headless boot|display unit skipped by \`ExecCondition\`; controller up (current boot)"
  "two-holders|native|Two runs on the Pi|a second \`pi_lock.sh run\` queues behind the holder"
  "power-cut|owner|Power cut|as a controller kill; state intact (after a fresh SD image)"
  "clean-reboot|owner|Clean reboot|services active, words within \`first_word_after_boot_s\`"
  "wifi-only|owner|Wi-Fi only|cable unplugged and a reboot: \`ssh pi\` works; NTP; a life starts"
  "laptop-off|owner|Laptop off|Pi keeps internet and time; the life continues"
  "password-over-wifi|owner|Password login over Wi-Fi|refused over Wi-Fi; accepted over the cable"
  "hostname-persistence|owner|Hostname persistence|still \`epitaph\` after two reboots"
)

row_field() { local IFS='|'; read -r -a f <<<"$1"; printf '%s' "${f[$2]}"; }

if [ "$LIST" = 1 ]; then
  for r in "${ROWS[@]}"; do printf '%-26s %-7s %s\n' "$(row_field "$r" 0)" "$(row_field "$r" 1)" "$(row_field "$r" 2)"; done
  exit 0
fi

SELECTED=()
if [ -n "$ONLY" ]; then
  IFS=',' read -r -a WANT <<<"$ONLY"
  for w in "${WANT[@]}"; do
    hit=""
    for r in "${ROWS[@]}"; do [ "$(row_field "$r" 0)" = "$w" ] && hit="$r"; done
    [ -n "$hit" ] || { echo "fault_matrix_pi: unknown row $w (see --list)" >&2; exit 2; }
    SELECTED+=("$hit")
  done
else
  SELECTED=("${ROWS[@]}")
fi
RUNNABLE=0
for r in "${SELECTED[@]}"; do [ "$(row_field "$r" 1)" = owner ] || RUNNABLE=$((RUNNABLE + 1)); done

FAULT_PI="${FAULT_PI:-$ROOT/tools/fault_pi.sh}"
SSH="${EPITAPH_SSH:-ssh}"
STAMP="$(date +%Y%m%d-%H%M%S)"
[ -n "$OUT" ] || OUT="$ROOT/logs/pi/faults-$STAMP.md"
LOGS="${OUT%.md}"
log() { printf '[fault_matrix %s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }

if [ "$DRY" = 0 ] && [ "${EPITAPH_PI_LOCKED:-}" != 1 ] && [ "$RUNNABLE" -gt 0 ]; then
  minutes=$(( RUNNABLE * ROW_MIN + 10 ))
  log "taking the Pi lock for up to ${minutes} min as $HOLDER ($RUNNABLE row(s))"
  exec "$ROOT/tools/pi_lock.sh" run "$HOLDER" "$minutes" -- \
    env EPITAPH_PI_LOCKED=1 "$0" "${ARGS[@]}" --out "$OUT"
fi

if [ "$DRY" = 1 ]; then
  HOST="${PI_HOST:-pi}"
  for r in "${SELECTED[@]}"; do
    name="$(row_field "$r" 0)"
    case "$(row_field "$r" 1)" in
      pi) printf '+ EPITAPH_PI_LOCKED=1 timeout %sm %s %s\n' "$ROW_MIN" "$FAULT_PI" "$name" >&2 ;;
      native) printf '+ (native) %s\n' "$name" >&2 ;;
      owner) printf '  owner: %s\n' "$name" >&2 ;;
    esac
  done
  log "dry run: nothing ran; the table would go to $OUT"
  exit 0
fi

# shellcheck source=tools/pi_host.sh
. "$ROOT/tools/pi_host.sh"
HOST="$(pi_host)" || { log "FAIL: the Pi does not answer"; exit 1; }
remote() { "$SSH" -o BatchMode=yes -o ConnectTimeout=10 "$HOST" "$@"; }
mkdir -p "$LOGS"

WAS_ACTIVE="$(remote systemctl is-active epitaph-controller 2>/dev/null || true)"
log "host $HOST; epitaph-controller ${WAS_ACTIVE:-unknown}; logs in $LOGS"
restore_controller() {
  if [ "$WAS_ACTIVE" = active ]; then
    now="$(remote systemctl is-active epitaph-controller 2>/dev/null || true)"
    if [ "$now" != active ]; then
      log "epitaph-controller is $now after the rows: starting it again"
      remote sudo -n systemctl start epitaph-controller || log "WARNING: epitaph-controller did not start"
    fi
  fi
}
trap restore_controller EXIT

# --- the rows --------------------------------------------------------------------------------
RESULTS=()   # name|result|seconds|line
FAIL=0
native_row() {
  case "$1" in
    headless-boot)
      if PI_HOST="$HOST" EPITAPH_PI_LOCKED=1 "$ROOT/tools/headless_boot_check.sh" --holder "$HOLDER"; then
        echo "PASS: headless boot checks pass on the current boot (no reboot)"
      else
        echo "FAIL: headless boot check failed"; return 1
      fi ;;
    two-holders)
      # This driver holds the Pi lock: a second request must see the holder and wait.
      set +e
      msg="$(EPITAPH_LOCK_WAIT_S=2 "$ROOT/tools/pi_lock.sh" run "$HOLDER-probe" 1 -- true 2>&1)"
      rc=$?
      set -e
      echo "$msg"
      if [ "$rc" = 75 ] && grep -q 'held by' <<<"$msg"; then
        echo "PASS: a second lock request saw the holder, waited and gave up after 2 s"
      else
        echo "FAIL: a second lock request did not queue (exit $rc)"; return 1
      fi ;;
    *) echo "FAIL: no native row $1"; return 1 ;;
  esac
}

for r in "${SELECTED[@]}"; do
  name="$(row_field "$r" 0)"; how="$(row_field "$r" 1)"
  rlog="$LOGS/$name.log"
  if [ "$how" = owner ]; then
    RESULTS+=("$name|owner|-|needs the owner or a fresh SD image")
    continue
  fi
  log "row $name ($how)"
  start=$SECONDS
  set +e
  if [ "$how" = native ]; then
    native_row "$name" > "$rlog" 2>&1
    rc=$?
  elif [ ! -x "$FAULT_PI" ]; then
    echo "no executable $FAULT_PI" > "$rlog"
    rc=2
  else
    EPITAPH_PI_LOCKED=1 EPITAPH_LOCK_WAIT_S=1 PI_HOST="$HOST" HOLDER="$HOLDER" \
      timeout --kill-after=30 "$((ROW_MIN * 60))" "$FAULT_PI" "$name" > "$rlog" 2>&1
    rc=$?
  fi
  set -e
  secs=$((SECONDS - start))
  line="$(grep -E '^(PASS|FAIL)\b' "$rlog" | tail -n 1 || true)"
  case "$rc" in
    0) if [[ "$line" == PASS* ]]; then result=PASS; else result=error; line="exit 0 without a PASS line"; fi ;;
    1) result=FAIL; [ -n "$line" ] || line="exit 1 without a FAIL line" ;;
    2) result="not run"; line="${line:-fault_pi.sh does not know this row (exit 2): $(tail -n 1 "$rlog")}" ;;
    75) result=error; line="fault_pi.sh took the Pi lock itself (exit 75); it must honour EPITAPH_PI_LOCKED=1" ;;
    124|137) result=FAIL; line="timed out after ${ROW_MIN} min" ;;
    *) result=error; line="exit $rc: ${line:-$(tail -n 1 "$rlog")}" ;;
  esac
  [ "$result" = PASS ] || FAIL=1
  log "row $name: $result (${secs}s) $line"
  RESULTS+=("$name|$result|$secs|$line")
done

# --- after the rows ----------------------------------------------------------------------------
restore_controller
trap - EXIT
POST="$(remote bash -s <<'SH' 2>/dev/null || true
echo "controller=$(systemctl is-active epitaph-controller 2>/dev/null) restarts=$(systemctl show -p NRestarts --value epitaph-controller 2>/dev/null)"
echo "clock_khz=$(cat /sys/devices/system/cpu/cpufreq/policy0/scaling_max_freq 2>/dev/null)"
echo "units=$(systemctl list-units --plain --no-legend --state=active,activating 'epitaph-pilife-*' 'epitaph-fault-*' 'llama-*' 2>/dev/null | awk '{print $1}' | tr '\n' ' ')"
echo "throttled=$(vcgencmd get_throttled 2>/dev/null | cut -d= -f2)"
SH
)"
COMMIT="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
FPI_SUM="$( [ -f "$FAULT_PI" ] && sha256sum "$FAULT_PI" | cut -c1-12 || echo missing)"
{
  echo "# Fault matrix on the Pi ($STAMP)"
  echo
  echo "Host \`$HOST\`, commit \`$COMMIT\`, \`fault_pi.sh\` sha256 \`$FPI_SUM\`, holder $HOLDER, $ROW_MIN min per row."
  echo "Logs in \`$(basename "$LOGS")/\`."
  echo
  echo "| Row | Fault | Expected | Result | Seconds | Evidence |"
  echo "|---|---|---|---|---|---|"
  for res in "${RESULTS[@]}"; do
    IFS='|' read -r name result secs line <<<"$res"
    for r in "${ROWS[@]}"; do
      [ "$(row_field "$r" 0)" = "$name" ] || continue
      fault="$(row_field "$r" 2)"; expected="$(row_field "$r" 3)"
    done
    echo "| \`$name\` | $fault | $expected | **$result** | $secs | ${line//|/\\|} |"
  done
  echo
  echo "After the rows: $(printf '%s' "$POST" | tr '\n' ';' | sed 's/;$//; s/;/; /g')"
} > "$OUT"
if [ "$FAIL" = 0 ]; then
  log "PASS: every row that ran passed; table in $OUT"
else
  log "FAIL: see $OUT"
fi
exit "$FAIL"
