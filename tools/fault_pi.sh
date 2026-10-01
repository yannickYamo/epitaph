#!/usr/bin/env bash
# The Pi rows of the fault matrix that part C owns (BUILD_PLAN 10.4, docs/GATES.md), against the
# installed controller service. Each row prints its evidence and ends with one line:
# `PASS <row>: ...` or `FAIL <row>: ...`; the exit status is 0 only when every row passed.
#
#   tools/pi_lock.sh run <agent> 60 -- tools/fault_pi.sh <row>... [--dry-run]
#
# Rows (run in the order given; `all` = netblock two-controllers crash controller-kill hang):
#   netblock         the creature cgroup's nftables rule names its cgroup id; from inside the
#                    cgroup an outbound TCP connect is refused while 127.0.0.1 answers
#   two-controllers  `epitaph run` beside the service refuses, points to `epitaph ctl new-life`,
#                    and the service and its life go on untouched
#   crash            kill -9 of the creature: cause=crash, then the next life is born
#   hang             kill -STOP of the creature while it works (CPU busy): cause=hang after the
#                    timeout, the stopped process is gone (cgroup.kill), the next life is born
#   controller-kill  systemctl kill -s KILL epitaph-controller: systemd restarts it, the life is
#                    closed as interrupted, the counter goes on (+1), one creature, full clock,
#                    the network rule loaded again on the new cgroup
#
# Rows that need a creature wait for one (state `living`); when too little of the life is left
# for the row, they start a fresh life with `epitaph ctl new-life` (the old one ends as manual).
# The script never stops the controller; whatever happens, it leaves it running (trap), the
# stopped creature continued, and nothing of its own behind.
#
# --dry-run prints every remote command without the lock or the Pi. PI_HOST picks the alias
# (else tools/pi_host.sh: `pi`, then `pi-eth`).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EPITAPH=/opt/epitaph/venv/bin/epitaph
STATE=/var/lib/epitaph
UNIT=epitaph-controller
CGREL=system.slice/$UNIT.service/creature
CG=/sys/fs/cgroup/$CGREL
NETBLOCK=/usr/local/sbin/epitaph-netblock
PROBE_TARGET="${PROBE_TARGET:-1.1.1.1:443}"
LLAMA_PORT="${LLAMA_PORT:-8081}"
WAIT_LIVING_S="${WAIT_LIVING_S:-900}"   # a load, a silence, a slow first thought
BIRTH_S="${BIRTH_S:-600}"               # silence 90 s + load up to 300 s + margin
HANG_S="${HANG_S:-900}"                 # first-token limit or token gap (120 s) + margin

DRY=0; ROWS=()
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    all) ROWS+=(netblock two-controllers crash controller-kill hang) ;;
    netblock|two-controllers|crash|hang|controller-kill) ROWS+=("$a") ;;
    # the row names of tools/fault_matrix_pi.sh (BUILD_PLAN 10.4 wording)
    creature-network) ROWS+=(netblock) ;;
    controller-killed) ROWS+=(controller-kill) ;;
    -h|--help) sed -n '2,/^set -euo/{/^#/p}' "$0"; exit 0 ;;
    *) echo "unknown row or flag: $a (see --help)" >&2; exit 2 ;;
  esac
done
[ "${#ROWS[@]}" -gt 0 ] || { echo "usage: $0 <row>... [--dry-run] (see --help)" >&2; exit 2; }

# --- the Pi lock must be held by our caller (as tools/pi_deploy.sh) ----------------------------
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

if [ "$DRY" = 1 ]; then
  HOST="${PI_HOST:-pi}"
else
  if ! lock_held_by_ancestor; then
    echo "fault_pi.sh must run under the Pi lock: tools/pi_lock.sh run <agent> 60 -- $0 $*" >&2
    exit 2
  fi
  # shellcheck source=tools/pi_host.sh
  . "$ROOT/tools/pi_host.sh"
  HOST="$(pi_host)" || exit 3
fi

say() {
  if [ "$DRY" = 1 ]; then  # nothing measured: no verdict
    case "$*" in PASS* | FAIL*) printf '  (dry run: no verdict)\n'; return 0 ;; esac
  fi
  printf '%s\n' "$*"
}
ev() { printf '  %s\n' "$*"; }

# r CMD: run CMD on the Pi and print its output (in a dry run: print CMD, output nothing).
r() {
  if [ "$DRY" = 1 ]; then printf '  [dry] ssh %s %q\n' "$HOST" "$1" >&2; return 0; fi
  ssh -o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=15 "$HOST" "$1"
}

# --- reading the Pi --------------------------------------------------------------------------

# status FIELD...: fields of `epitaph ctl status`, space-separated ("-" when absent).
status() {
  local json
  if [ "$DRY" = 1 ]; then  # a creature 100 s into life 1 of 1800 s
    local k out=()
    for k in "$@"; do
      case "$k" in state) out+=(living) ;; life) out+=(1) ;; t) out+=(100) ;;
        lifespan_s) out+=(1800) ;; *) out+=(-) ;; esac
    done
    echo "${out[*]}"; return 0
  fi
  json="$(r "$EPITAPH ctl status" 2>/dev/null || echo '{}')"
  printf '%s' "$json" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except ValueError:
    d = {}
print(" ".join(str(d.get(k, "-")) for k in sys.argv[1:]))' "$@"
}

creature_pid() {
  if [ "$DRY" = 1 ]; then echo 4242; return 0; fi
  r "for p in \$(cat $CG/cgroup.procs 2>/dev/null); do
       [ \"\$(cat /proc/\$p/comm 2>/dev/null)\" = llama-server ] && echo \$p && break; done; true"
}

lifedir() { printf '%s/lives/%06d' "$STATE" "$1"; }

# death_cause N: the cause in life N's death record, "-" while it has none.
death_cause() {
  if [ "$DRY" = 1 ]; then echo "$EXPECT_CAUSE"; return 0; fi
  r "cat $(lifedir "$1")/death.json 2>/dev/null" | python3 -c '
import json, sys
try:
    print(json.load(sys.stdin).get("cause", "-"))
except ValueError:
    print("-")'
}

unit_show() {  # unit_show PROP: one property of the controller unit
  if [ "$DRY" = 1 ]; then echo 0; return 0; fi
  r "systemctl show $UNIT -p $1 --value"
}

# wait_for SECONDS CMD...: run CMD every 3 s until it succeeds; 1 on timeout.
wait_for() {
  local limit=$1; shift
  local t0=$SECONDS
  until "$@"; do
    [ $((SECONDS - t0)) -lt "$limit" ] || return 1
    [ "$DRY" = 1 ] && return 0
    sleep 3
  done
}

has_death() { [ "$(death_cause "$1")" != - ]; }             # life $1 has its death record
born_after() { [ "$(status life)" != "$1" ] && is_living; }  # a later life than $1 is living
restarted() {                                                 # NRestarts moved on from $1
  [ "$(unit_show NRestarts)" != "$1" ] && [ "$(r "systemctl is-active $UNIT")" = active ]
}

is_living() {
  local st
  read -r st _ <<<"$(status state)"
  [ "$st" = living ] && [ -n "$(creature_pid)" ]
}

# need_life SECONDS_LEFT [MAX_T]: a living creature with at least SECONDS_LEFT of its life to
# go (and, if given, at most MAX_T seconds into it); otherwise a fresh life through ctl new-life.
need_life() {
  local left=$1 max_t=${2:-} st life t span
  read -r st life t span <<<"$(status state life t lifespan_s)"
  if [ "$st" = living ] && [ "$t" != - ] \
     && python3 -c 'import sys; t,s,l,m=sys.argv[1:]; sys.exit(0 if float(s)-float(t)>=float(l) and (not m or float(t)<=float(m)) else 1)' \
          "$t" "$span" "$left" "$max_t"; then
    return 0
  fi
  if [ "$st" = living ] || [ "$st" = reloading ]; then
    ev "life $life at t=$t of $span s: starting a fresh one (epitaph ctl new-life)"
    r "$EPITAPH ctl new-life >/dev/null" || true
    sleep 5
  fi
  wait_for "$WAIT_LIVING_S" is_living || return 1
  # the fresh life may be the one we asked for, or a later one; check the window again
  read -r st life t span <<<"$(status state life t lifespan_s)"
  ev "living: life $life at t=$t s of $span s"
}

# --- cleanup: the controller is always left running ------------------------------------------
STOPPED_PID=""
cleanup() {
  [ "$DRY" = 1 ] && return 0
  if [ -n "$STOPPED_PID" ]; then r "sudo -n kill -CONT $STOPPED_PID 2>/dev/null; true" || true; fi
  if [ "$(r "systemctl is-active $UNIT" 2>/dev/null || true)" != active ]; then
    echo "  cleanup: $UNIT not active; starting it" >&2
    r "sudo -n systemctl start $UNIT" || true
  fi
}
trap cleanup EXIT

# --- the rows ------------------------------------------------------------------------------------

row_netblock() {
  local ino rules inside outside out lo ok=1
  if ! r "test -d $CG" && [ "$DRY" = 0 ]; then say "FAIL netblock: no creature cgroup $CG"; return 1; fi
  ino="$(r "stat -c %i $CG")"
  rules="$(r "sudo -n $NETBLOCK status" || true)"
  ev "creature cgroup $CGREL, id $ino"
  if printf '%s\n' "$rules" | grep -Eq "cgroupv2 level 3 (\"$CGREL\"|$ino) .*comment \"$CGREL\""; then
    ev "nft rule: $(printf '%s\n' "$rules" | grep -c "comment \"$CGREL\"") rules for this cgroup"
  else
    ev "nft rule: none for this cgroup"; ok=0
  fi
  local probe="import json, socket
def c(addr):
    s = socket.socket(); s.settimeout(4)
    try:
        s.connect(addr); return 'connected'
    except OSError as e:
        return type(e).__name__
    finally:
        s.close()
srv = socket.socket(); srv.bind(('127.0.0.1', 0)); srv.listen(1)
h, p = '$PROBE_TARGET'.rsplit(':', 1)
print(json.dumps({'out': c((h, int(p))), 'lo': c(srv.getsockname()),
                  'llama': c(('127.0.0.1', $LLAMA_PORT))}))"
  inside="$(r "sudo -n sh -c 'echo \$\$ > $CG/cgroup.procs && exec timeout 20 python3 -c \"\$0\"' $(printf '%q' "$probe")" || true)"
  outside="$(r "timeout 20 python3 -c $(printf '%q' "$probe")" || true)"
  ev "inside the creature cgroup: $inside"
  ev "outside (ssh session):      $outside"
  out="$(printf '%s' "$inside" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("out"))' 2>/dev/null || echo "?")"
  lo="$(printf '%s' "$inside" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("lo"))' 2>/dev/null || echo "?")"
  [ "$out" != connected ] && [ "$out" != "?" ] || ok=0
  [ "$lo" = connected ] || ok=0
  if [ "$ok" = 1 ]; then
    say "PASS netblock: rule on cgroup id $ino; creature -> $PROBE_TARGET $out, -> 127.0.0.1 $lo"
  else
    say "FAIL netblock: rule/out/lo = $(printf '%s' "$rules" | grep -c "comment \"$CGREL\"" || true)/$out/$lo"
    return 1
  fi
}

row_two_controllers() {
  local pid0 life0 out pid1 life1 st
  pid0="$(unit_show MainPID)"; read -r life0 <<<"$(status life)"
  out="$(r "cd / && timeout 60 $EPITAPH run --port 7798 2>&1; echo rc=\$?" || true)"
  ev "second controller: $(printf '%s' "$out" | tr '\n' ' ' | cut -c1-300)"
  pid1="$(unit_show MainPID)"; read -r st life1 <<<"$(status state life)"
  ev "service MainPID $pid0 -> $pid1; life $life0 -> $life1 ($st)"
  if printf '%s' "$out" | grep -q 'rc=1' && printf '%s' "$out" | grep -q 'epitaph ctl new-life' \
     && [ "$pid0" = "$pid1" ] && [ "$life0" = "$life1" ]; then
    say "PASS two-controllers: refused (rc=1, points to epitaph ctl new-life); service untouched"
  else
    say "FAIL two-controllers: see the evidence above"; return 1
  fi
}

row_crash() {
  local life pid cause t0 dt life2 pid2
  need_life 240 || { say "FAIL crash: no living creature"; return 1; }
  read -r life <<<"$(status life)"; pid="$(creature_pid)"
  ev "life $life, creature pid $pid: kill -9"
  r "sudo -n kill -9 $pid"
  t0=$SECONDS; EXPECT_CAUSE=crash
  wait_for 120 has_death "$life" || true
  cause="$(death_cause "$life")"; dt=$((SECONDS - t0))
  ev "life $life death.json cause=$cause after ${dt}s"
  wait_for "$BIRTH_S" born_after "$life" || true
  read -r life2 <<<"$(status life)"; pid2="$(creature_pid)"
  ev "next: life $life2, creature pid $pid2, $((SECONDS - t0))s after the kill"
  if [ "$cause" = crash ] && [ "$life2" != "$life" ] && [ -n "$pid2" ] && [ "$pid2" != "$pid" ]; then
    say "PASS crash: cause=crash; life $life2 born $((SECONDS - t0))s after the kill"
  else
    say "FAIL crash: cause=$cause, next life $life2"; return 1
  fi
}

# busy: the creature is working on a request (prompt or tokens): more than 1.5 s of CPU in 2 s.
# The transcript cannot say so: events.jsonl is flushed once per thought, so a gen_start is on
# disk only after its thought has ended.
busy() {
  [ "$DRY" = 1 ] && return 0
  local used
  used="$(r "a=\$(sed -n 's/^usage_usec //p' $CG/cpu.stat); sleep 2;
             b=\$(sed -n 's/^usage_usec //p' $CG/cpu.stat); echo \$((b - a))")"
  [ -n "$used" ] && [ "$used" -gt 1500000 ]
}

row_hang() {
  local life pid cause t0 dt gone life2
  need_life 900 200 || { say "FAIL hang: no living creature"; return 1; }
  read -r life <<<"$(status life)"
  wait_for 600 busy || { say "FAIL hang: the creature of life $life never got busy"; return 1; }
  pid="$(creature_pid)"
  ev "life $life, creature busy (a request in flight), pid $pid: kill -STOP"
  r "sudo -n kill -STOP $pid"; STOPPED_PID=$pid
  t0=$SECONDS; EXPECT_CAUSE=hang
  wait_for "$HANG_S" has_death "$life" || true
  cause="$(death_cause "$life")"; dt=$((SECONDS - t0))
  gone="$(r "if kill -0 $pid 2>/dev/null; then echo alive; else echo gone; fi")"
  [ "$gone" = gone ] && STOPPED_PID=""
  ev "life $life death.json cause=$cause after ${dt}s; stopped pid $pid $gone"
  wait_for "$BIRTH_S" born_after "$life" || true
  read -r life2 <<<"$(status life)"
  ev "next: life $life2, $((SECONDS - t0))s after the stop"
  if [ "$cause" = hang ] && [ "$gone" = gone ] && [ "$life2" != "$life" ]; then
    say "PASS hang: cause=hang ${dt}s after kill -STOP; process killed; life $life2 born"
  else
    say "FAIL hang: cause=$cause after ${dt}s, process $gone, next life $life2"; return 1
  fi
}

row_controller_kill() {
  local life n0 pid0 n1 pid1 cause life2 count mhz ino rules ok=1
  need_life 240 || { say "FAIL controller-kill: no living creature"; return 1; }
  read -r life <<<"$(status life)"
  n0="$(unit_show NRestarts)"; pid0="$(unit_show MainPID)"
  ev "life $life, controller pid $pid0, NRestarts $n0: systemctl kill -s KILL $UNIT"
  r "sudo -n systemctl kill -s KILL $UNIT"
  wait_for 90 restarted "$n0" || true
  n1="$(unit_show NRestarts)"; pid1="$(unit_show MainPID)"
  ev "restarted: pid $pid0 -> $pid1, NRestarts $n0 -> $n1"
  EXPECT_CAUSE=interrupted
  wait_for 120 has_death "$life" || true
  cause="$(death_cause "$life")"
  ev "life $life death.json cause=$cause"
  wait_for "$BIRTH_S" born_after "$life" || true
  read -r life2 <<<"$(status life)"
  count="$(r "pgrep -xc llama-server || true")"
  mhz="$(r "cat /sys/devices/system/cpu/cpufreq/policy0/scaling_max_freq")"
  ino="$(r "stat -c %i $CG")"
  rules="$(r "sudo -n $NETBLOCK status" || true)"
  ev "next: life $life2; llama-server processes: $count; clock cap $mhz kHz"
  if printf '%s\n' "$rules" | grep -Eq "cgroupv2 level 3 (\"$CGREL\"|$ino) "; then
    ev "network rule loaded again on the new creature cgroup (id $ino)"
  else
    ev "network rule: NONE on the new creature cgroup (id $ino)"; ok=0
  fi
  [ "$n1" != "$n0" ] && [ "$pid1" != "$pid0" ] || ok=0
  [ "$cause" = interrupted ] || ok=0
  [ "$life2" = "$((life + 1))" ] || ok=0
  [ "$count" = 1 ] || ok=0
  if [ "$ok" = 1 ]; then
    say "PASS controller-kill: restarted (NRestarts $n0 -> $n1); life $life interrupted; life $life2 next; one creature"
  else
    say "FAIL controller-kill: see the evidence above"; return 1
  fi
}

FAILED=0
for row in "${ROWS[@]}"; do
  say "== fault $row ($(date -Is))"
  EXPECT_CAUSE=-
  if ! "row_${row//-/_}"; then FAILED=$((FAILED + 1)); fi
done
if [ "$DRY" = 1 ]; then  # nothing was measured: the verdicts above mean nothing
  say "dry run: ${#ROWS[@]} row(s) printed, nothing run"
  exit 0
fi
say "faults: ${#ROWS[@]} run, $FAILED failed"
[ "$FAILED" = 0 ]
