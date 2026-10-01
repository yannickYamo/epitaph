#!/usr/bin/env bash
# Sample the Pi during the soak, for tools/soak_report.py (card E7; gate G3, BUILD_PLAN 11.4).
#
#   tools/soak_sample.sh [options]
#     --out FILE     append the samples here (default logs/pi/soak-samples.tsv)
#     --every S      seconds between samples (default 600)
#     --count N      stop after N samples (default: run until killed)
#     --local        sample this machine (run it on the Pi itself) instead of over SSH
#     --dry-run      print the probe and the command; no SSH, nothing appended
#
# One tab-separated line per sample, under a `#` header written when the file is new:
#   epoch pid rss_kb temp_c throttled state_kb root_used_kb nrestarts
# epoch is the Pi's clock (NTP; the same clock as the events' ts); pid and nrestarts are the
# controller unit's MainPID and NRestarts; rss_kb its VmRSS; temp_c from thermal_zone0;
# throttled the `vcgencmd get_throttled` value; state_kb `du -sk` of the state directory;
# root_used_kb the root filesystem's used space. A value the Pi cannot give is `-`.
#
# Read-only on the Pi, so it takes no Pi lock (BUILD_PLAN 8.3): it only reads /proc, /sys,
# systemctl and du. A sample the Pi does not answer is skipped with a line on stderr, and the
# report lists the hole. Run it in the background on the laptop for the whole soak, e.g.
#   nohup tools/soak_sample.sh --out logs/pi/soak-samples.tsv >/dev/null 2>logs/pi/soak-sample.log &
#
# Environment: PI_HOST (skip the alias probe), EPITAPH_PI_STATE (default /var/lib/epitaph),
# EPITAPH_SSH (default ssh), EPITAPH_UNIT (default epitaph-controller).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/logs/pi/soak-samples.tsv"
EVERY=600
COUNT=0
LOCAL=0
DRY=0
usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; }
while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT="${2:?--out needs a file}"; shift 2 ;;
    --every) EVERY="${2:?--every needs seconds}"; shift 2 ;;
    --count) COUNT="${2:?--count needs a number}"; shift 2 ;;
    --local) LOCAL=1; shift ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "soak_sample: unknown option $1" >&2; usage >&2; exit 2 ;;
  esac
done
[[ "$EVERY" =~ ^[1-9][0-9]*$ ]] || { echo "soak_sample: --every must be whole seconds" >&2; exit 2; }
[[ "$COUNT" =~ ^[0-9]+$ ]] || { echo "soak_sample: --count must be a number" >&2; exit 2; }

SSH="${EPITAPH_SSH:-ssh}"
STATE="${EPITAPH_PI_STATE:-/var/lib/epitaph}"
UNIT="${EPITAPH_UNIT:-epitaph-controller}"
log() { printf '[soak_sample %s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }

# The probe runs on the Pi with bash; arguments: state dir, unit. It prints one line. Its $
# expand on the Pi, not here, hence the single quotes.
# shellcheck disable=SC2016
PROBE='
state="$1"; unit="$2"
v() { [ -n "$1" ] && printf "%s" "$1" || printf -- "-"; }
pid=$(systemctl show -p MainPID --value "$unit" 2>/dev/null || true)
nr=$(systemctl show -p NRestarts --value "$unit" 2>/dev/null || true)
rss=""
if [ -n "$pid" ] && [ "$pid" != 0 ] && [ -r "/proc/$pid/status" ]; then
  rss=$(sed -n "s/^VmRSS:[[:space:]]*\([0-9]*\).*/\1/p" "/proc/$pid/status")
fi
[ "$pid" = 0 ] && pid=""
temp=""
if [ -r /sys/class/thermal/thermal_zone0/temp ]; then
  t=$(cat /sys/class/thermal/thermal_zone0/temp)
  temp=$(printf "%d.%d" "$((t / 1000))" "$(((t % 1000) / 100))")
fi
thr=$(vcgencmd get_throttled 2>/dev/null | sed -n "s/^throttled=//p" || true)
st=$(du -sk "$state" 2>/dev/null | cut -f1 || true)
root=$(df -Pk / 2>/dev/null | tail -n 1 | tr -s " " | cut -d" " -f3 || true)
printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" "$(date +%s)" "$(v "$pid")" "$(v "$rss")" \
  "$(v "$temp")" "$(v "$thr")" "$(v "$st")" "$(v "$root")" "$(v "$nr")"
'

probe() {
  if [ "$LOCAL" = 1 ]; then
    bash -c "$PROBE" probe "$STATE" "$UNIT"
  else
    local host
    host="${PI_HOST:-}"
    if [ -z "$host" ]; then
      # shellcheck source=tools/pi_host.sh
      . "$ROOT/tools/pi_host.sh"
      host="$(pi_host)" || return 1
    fi
    "$SSH" -o BatchMode=yes -o ConnectTimeout=10 "$host" \
      "bash -c $(printf '%q' "$PROBE") probe $(printf '%q' "$STATE") $(printf '%q' "$UNIT")"
  fi
}

if [ "$DRY" = 1 ]; then
  where="$([ "$LOCAL" = 1 ] && echo "this machine" || echo "${PI_HOST:-pi (probed)}")"
  log "dry run: every ${EVERY}s on $where, appending to $OUT"
  printf '%s\n' "$PROBE"
  exit 0
fi

mkdir -p "$(dirname "$OUT")"
[ -s "$OUT" ] || printf '# epoch\tpid\trss_kb\ttemp_c\tthrottled\tstate_kb\troot_used_kb\tnrestarts\n' >> "$OUT"
n=0
while :; do
  if line="$(probe)" && [ -n "$line" ]; then
    printf '%s\n' "$line" >> "$OUT"
  else
    log "no answer from the Pi: sample skipped"
  fi
  n=$((n + 1))
  if [ "$COUNT" -gt 0 ] && [ "$n" -ge "$COUNT" ]; then break; fi
  sleep "$EVERY"
done
