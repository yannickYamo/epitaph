#!/usr/bin/env bash
# Gather evidence from the running installation without stopping it (card E6; gate G2.3).
#
#   tools/collect_lives.sh [options]
#     --lives N              the newest N finished lives (default 3)
#     --out DIR              where they go (default logs/pi/service-<stamp>)
#     --profile P            only lives that ran profile P (from their meta.json), judged as P
#                            (default: any profile, each judged as the one it recorded)
#     --hardware H           overlay to judge by (default: the one each life recorded)
#     --include-interrupted  count lives closed as `interrupted` (a deploy or a restart cut
#                            them short; they fail `cause`); by default they are listed and
#                            skipped
#     --dry-run              print the commands; no SSH, no verify
#
# Read-only on the Pi, so it takes no Pi lock (BUILD_PLAN 8.3: the lock serialises work that
# changes or loads the Pi; this only lists and copies files). It:
#   1. finds the Pi (tools/pi_host.sh) and lists <state>/lives: a life is finished when its
#      death.json exists; the life in progress has none and is never judged;
#   2. picks the newest N finished lives (skipping interrupted ones unless asked) and copies
#      them, each with the life after it when there is one (even the life in progress: its
#      birth feeds the earlier life's next_birth check), to DIR/lives/NNNNNN;
#   3. runs `epitaph verify-life` on each picked life at its profile's level, which writes
#      verify.json next to its events.jsonl, and writes DIR/summary.md.
# Exit: 0 when N lives were found and every one passes; 1 when one fails or fewer than N
# finished lives exist; 2 usage.
#
# Environment: PI_HOST (skip the alias probe), EPITAPH_PI_STATE (default /var/lib/epitaph),
# EPITAPH_PYTHON (laptop Python, default .venv/bin/python), EPITAPH_SSH (default ssh).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
N=3
OUT=""
PROFILE=""
HARDWARE=""
INTERRUPTED=0
DRY=0
usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; }
while [ $# -gt 0 ]; do
  case "$1" in
    --lives) N="${2:?--lives needs a value}"; shift 2 ;;
    --out) OUT="${2:?--out needs a value}"; shift 2 ;;
    --profile) PROFILE="${2:?--profile needs a value}"; shift 2 ;;
    --hardware) HARDWARE="${2:?--hardware needs a value}"; shift 2 ;;
    --include-interrupted) INTERRUPTED=1; shift ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "collect_lives: unknown option $1" >&2; usage >&2; exit 2 ;;
  esac
done
[[ "$N" =~ ^[1-9][0-9]?$ ]] || { echo "collect_lives: --lives must be 1-99" >&2; exit 2; }
[ -z "$PROFILE" ] || [[ "$PROFILE" =~ ^[A-Za-z0-9_./-]+$ ]] || {
  echo "collect_lives: bad profile name" >&2; exit 2; }

PYTHON="${EPITAPH_PYTHON:-$ROOT/.venv/bin/python}"
SSH="${EPITAPH_SSH:-ssh}"
STATE="${EPITAPH_PI_STATE:-/var/lib/epitaph}"
pyrun() { PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON" "$@"; }
log() { printf '[collect_lives %s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
quote() { local a out=""; for a in "$@"; do out+="$(printf '%q' "$a") "; done; printf '%s' "${out% }"; }

STAMP="$(date +%Y%m%d-%H%M%S)"
[ -n "$OUT" ] || OUT="$ROOT/logs/pi/service-$STAMP"

if [ "$DRY" = 1 ]; then
  HOST="${PI_HOST:-pi}"
else
  # shellcheck source=tools/pi_host.sh
  . "$ROOT/tools/pi_host.sh"
  HOST="$(pi_host)" || exit 1
fi
remote() {
  if [ "$DRY" = 1 ]; then printf '+ %s %s %s\n' "$SSH" "$HOST" "$(quote "$@")" >&2; return 0; fi
  "$SSH" -o BatchMode=yes -o ConnectTimeout=10 "$HOST" "$(quote "$@")"
}

# --- 1. what the service has recorded -----------------------------------------------------
# One line per life folder: "NNNNNN done <cause> <profile>" or "NNNNNN running - <profile>".
if [ "$DRY" = 1 ]; then
  printf '+ %s %s bash -s -- %s   # list the life folders and their causes\n' "$SSH" "$HOST" "$STATE" >&2
  LISTING="$(for i in $(seq 1 "$((N + 1))"); do printf '%06d done oom %s\n' "$i" "${PROFILE:-pi4/default}"; done)"
else
  LISTING="$("$SSH" -o BatchMode=yes -o ConnectTimeout=10 "$HOST" "bash -s -- $(quote "$STATE")" <<'SH'
cd "$1/lives" 2>/dev/null || exit 0
field() { sed -n "s/.*\"$1\": *\"\([^\"]*\)\".*/\1/p" "$2" 2>/dev/null | head -n 1; }
for d in $(ls | grep -E '^[0-9]+$' | sort); do
  p=$(field profile "$d/meta.json")
  if [ -f "$d/death.json" ]; then
    c=$(field cause "$d/death.json")
    echo "$d done ${c:-unknown} ${p:-unknown}"
  else
    echo "$d running - ${p:-unknown}"
  fi
done
SH
)" || { log "FAIL: cannot list $STATE/lives on $HOST"; exit 1; }
fi
mapfile -t ROWS < <(printf '%s\n' "$LISTING" | grep -E '^[0-9]+ ' || true)
log "host $HOST: ${#ROWS[@]} life folder(s) in $STATE/lives"

# --- 2. pick the newest N finished lives ---------------------------------------------------
PICK=()
SKIPPED=()
for ((i = ${#ROWS[@]} - 1; i >= 0 && ${#PICK[@]} < N; i--)); do
  read -r life state cause ran <<<"${ROWS[$i]}"
  if [ "$state" != "done" ]; then
    log "life $life is in progress: not judged"
  elif [ -n "$PROFILE" ] && [ "$ran" != "$PROFILE" ]; then
    log "life $life ran $ran, not $PROFILE: skipped"
  elif [ "$cause" = interrupted ] && [ "$INTERRUPTED" = 0 ]; then
    log "life $life was interrupted (a deploy or a restart): skipped (--include-interrupted)"
    SKIPPED+=("$life")
  else
    PICK=("$life" "${PICK[@]}")
  fi
done
[ "${#PICK[@]}" -gt 0 ] || { log "FAIL: no finished life to judge"; exit 1; }
# Each picked life with the folder after it (context for next_birth).
COPY=()
for life in "${PICK[@]}"; do
  for ((i = 0; i < ${#ROWS[@]}; i++)); do
    read -r n _ <<<"${ROWS[$i]}"
    [ "$n" = "$life" ] || continue
    COPY+=("$n")
    if [ $((i + 1)) -lt "${#ROWS[@]}" ]; then read -r nxt _ <<<"${ROWS[$((i + 1))]}"; COPY+=("$nxt"); fi
  done
done
mapfile -t COPY < <(printf '%s\n' "${COPY[@]}" | sort -u)
log "judging ${PICK[*]} (copying ${COPY[*]}) into $OUT"

# --- 3. copy, read-only on the Pi ----------------------------------------------------------
if [ "$DRY" = 1 ]; then
  printf '+ %s %s tar -C %s/lives -cf - %s | tar -C %s/lives -xf -\n' \
    "$SSH" "$HOST" "$STATE" "${COPY[*]}" "$OUT" >&2
else
  mkdir -p "$OUT/lives"
  # A life in progress grows while tar reads it (GNU tar exit 1); its torn last line is
  # ignored by verify-life.
  "$SSH" -o BatchMode=yes -o ConnectTimeout=10 "$HOST" \
    "tar -C $(printf '%q' "$STATE/lives") --warning=no-file-changed -cf - $(quote "${COPY[@]}"); rc=\$?; [ \$rc -le 1 ]" \
    | tar -C "$OUT/lives" -xf - || { log "FAIL: copy from $HOST failed"; exit 1; }
  remote systemctl show epitaph-controller -p ActiveState -p NRestarts -p ActiveEnterTimestamp \
    > "$OUT/controller.txt" 2>/dev/null || true
fi

# --- 4. judge them -------------------------------------------------------------------------
FAIL=0
[ "${#PICK[@]}" -ge "$N" ] || { log "FAIL: $N finished life(s) asked for, ${#PICK[@]} found"; FAIL=1; }
for life in "${PICK[@]}"; do
  cmd=(-m epitaph verify-life "$OUT/lives/$life")
  [ -z "$PROFILE" ] || cmd+=(--profile "$PROFILE")
  [ -z "$HARDWARE" ] || cmd+=(--hardware "$HARDWARE")
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
  exit 0
fi
pyrun - "$OUT" "${SKIPPED[*]:-}" "${PICK[@]}" > "$OUT/summary.md" <<'PY'
import json, sys
from pathlib import Path
out, skipped, lives = Path(sys.argv[1]), sys.argv[2].split(), sys.argv[3:]
print(f"# Lives collected from the service ({out.name})\n")
print("| Life | Profile | Level | Cause | Lived (s) | Result | Failed | Advisory |")
print("|---|---|---|---|---|---|---|---|")
for n in lives:
    f = out / "lives" / n / "verify.json"
    if not f.is_file():
        print(f"| {n} | | | | | no verify.json | | |")
        continue
    r = json.loads(f.read_text())
    m = r.get("metrics", {})
    print(f"| {n} | {r['profile']} | {r['level']} | {m.get('cause')} | {m.get('lived_s')} | "
          f"{'PASS' if r['ok'] else 'FAIL'} | {', '.join(r['failed'])} | "
          f"{', '.join(r['advisory'])} |")
if skipped:
    print(f"\nSkipped as interrupted: {', '.join(skipped)}.")
ctl = out / "controller.txt"
if ctl.is_file() and ctl.read_text().strip():
    print("\nController: " + "; ".join(ctl.read_text().splitlines()))
nums = [int(n) for n in lives]
if len(nums) > 1:
    run = nums == list(range(nums[0], nums[0] + len(nums)))
    print(f"\nConsecutive lives: {'yes' if run else 'no'} ({', '.join(lives)}).")
PY
if [ "$FAIL" = 0 ]; then
  log "PASS: ${#PICK[@]} life(s) from the service; evidence in $OUT (summary.md, verify.json per life)"
else
  log "FAIL: see the lines above; evidence in $OUT (summary.md)"
fi
exit "$FAIL"
