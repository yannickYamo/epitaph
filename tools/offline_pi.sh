#!/usr/bin/env bash
# Take the Pi off every network for a while, safely, to test the piece offline
# (docs/INSTALLATION.md "Offline"; the Pi test plan of the offline round).
#
#   tools/offline_pi.sh [options]
#     --minutes N    networking comes back on N minutes after the arming, and N minutes after
#                    any boot until it has (default 15; 5-240)
#     --reboot       also reboot once networking is off: the Pi boots with no network at all
#     --no-wait      return once networking is going down (default: wait for the Pi to come
#                    back, then report what it did offline)
#     --status       report the rescue and the networking state; change nothing
#     --disarm       networking on, then remove the rescue (a rescue armed by a run that
#                    stopped early, or a leftover)
#     --agent NAME   name on the Pi lock (default $AGENT, else L)
#     --dry-run      print the commands; no lock, no SSH
#
# Within one hold of the Pi lock it:
#   1. arms the rescue: epitaph-offline-rescue.{service,timer} in /etc/systemd/system. The
#      timer (OnActiveSec=N min, enabled for timers.target) fires N minutes from now, or N
#      minutes after a boot if the Pi reboots first; the service runs `nmcli networking on`
#      (again every 30 s until it succeeds), then disables its timer;
#   2. verifies it: both files exactly as written, `systemd-analyze verify` clean, the timer
#      enabled, active and due. Anything else stops here with exit 1, networking untouched;
#   3. takes networking down 5 s later from a transient unit, so this SSH session ends first:
#      `nmcli networking off` (NetworkManager keeps that across a reboot; the cable goes down
#      too), and with --reboot `systemctl reboot`. The Pi re-checks the rescue before it;
#   4. unless --no-wait: waits for the Pi to answer again (N minutes plus a margin), then
#      reports the rescue's journal, the boot, the controller, the display and the time sync.
# Every step prints what it did. Exit 0: done (and the Pi is back); 1: refused, failed, or the
# Pi did not come back in time; 2: usage.
#
# Environment: PI_HOST (skip the alias probe), EPITAPH_SSH (default ssh), EPITAPH_POLL_S
# (seconds between probes while offline, default 30), EPITAPH_PI_UNIT_DIR (default
# /etc/systemd/system), EPITAPH_OFFLINE_BUDGET_S (how long to wait for the Pi to come back),
# EPITAPH_OFFLINE_DOWN_S (how long it may still answer after networking off; tests).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARGS=("$@")
MINUTES=15
REBOOT=0
WAIT=1
ACTION=offline
AGENT="${AGENT:-L}"
DRY=0
usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; }
while [ $# -gt 0 ]; do
  case "$1" in
    --minutes) MINUTES="${2:?--minutes needs a value}"; shift 2 ;;
    --reboot) REBOOT=1; shift ;;
    --no-wait) WAIT=0; shift ;;
    --status) ACTION=status; shift ;;
    --disarm) ACTION=disarm; shift ;;
    --agent) AGENT="${2:?--agent needs a value}"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "offline_pi: unknown option $1" >&2; usage >&2; exit 2 ;;
  esac
done
if ! [[ "$MINUTES" =~ ^[0-9]{1,3}$ ]] || [ "$MINUTES" -lt 5 ] || [ "$MINUTES" -gt 240 ]; then
  echo "offline_pi: --minutes must be 5-240" >&2; exit 2
fi
SSH="${EPITAPH_SSH:-ssh}"
POLL_S="${EPITAPH_POLL_S:-30}"
UNIT_DIR="${EPITAPH_PI_UNIT_DIR:-/etc/systemd/system}"
log() { printf '[offline_pi %s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
exec 3>&2  # dry-run lines go here

# The lock covers the whole offline time when waiting, so nobody else queues work for a Pi
# that cannot answer.
if [ "$DRY" = 0 ] && [ "${EPITAPH_PI_LOCKED:-}" != 1 ]; then
  minutes=10
  if [ "$ACTION" = offline ] && [ "$WAIT" = 1 ]; then
    minutes=$(( MINUTES + 25 + REBOOT * 10 ))
  fi
  log "taking the Pi lock for ${minutes} min as $AGENT"
  exec "$ROOT/tools/pi_lock.sh" run "$AGENT" "$minutes" -- env EPITAPH_PI_LOCKED=1 "$0" "${ARGS[@]}"
fi

# shellcheck source=tools/pi_host.sh
. "$ROOT/tools/pi_host.sh"
find_host() { if [ "$DRY" = 1 ]; then printf '%s\n' "${PI_HOST:-pi}"; else pi_host 2>/dev/null; fi; }

# The part that runs on the Pi, as root: `bash -s -- ACTION MINUTES REBOOT UNIT_DIR`.
read -r -d '' PI_SCRIPT <<'PI' || true
set -eu
action="$1"; minutes="$2"; reboot="$3"; unit_dir="$4"
name=epitaph-offline-rescue
svc="$unit_dir/$name.service"; tmr="$unit_dir/$name.timer"
say() { printf '  %s\n' "$*"; }
nmcli_bin="$(command -v nmcli || true)"
systemctl_bin="$(command -v systemctl || true)"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT

units() {  # the two files, into $tmp
  cat > "$tmp/service" <<EOF
# Written by tools/offline_pi.sh: networking back on after an offline test.
# tools/offline_pi.sh --disarm removes it; it disables its own timer once it has run.
[Unit]
Description=epitaph offline test: networking back on
After=NetworkManager.service
Wants=NetworkManager.service
StartLimitIntervalSec=0

[Service]
Type=oneshot
ExecStart=$nmcli_bin networking on
ExecStartPost=$nmcli_bin networking
ExecStartPost=-$systemctl_bin disable --no-reload $name.timer
Restart=on-failure
RestartSec=30
EOF
  cat > "$tmp/timer" <<EOF
# Written by tools/offline_pi.sh: fires $minutes min after it starts (now, or at the next boot).
[Unit]
Description=epitaph offline test: networking back on after $minutes min

[Timer]
OnActiveSec=${minutes}min
AccuracySec=1s

[Install]
WantedBy=timers.target
EOF
}

verify() {  # the armed rescue is exactly ours and due; prints why not
  local bad=0 out state next
  units
  cmp -s "$tmp/service" "$svc" || { say "rescue check: $svc missing or not as written"; bad=1; }
  cmp -s "$tmp/timer" "$tmr" || { say "rescue check: $tmr missing or not as written (another --minutes?)"; bad=1; }
  if out="$(systemd-analyze verify "$svc" "$tmr" 2>&1)" && [ -z "$out" ]; then :
  else say "rescue check: systemd-analyze verify: $(printf '%s' "$out" | tr '\n' ' ')"; bad=1; fi
  state="$(systemctl is-enabled "$name.timer" 2>/dev/null || true)"
  [ "$state" = enabled ] || { say "rescue check: timer is-enabled=$state (a reboot would lose it)"; bad=1; }
  state="$(systemctl is-active "$name.timer" 2>/dev/null || true)"
  [ "$state" = active ] || { say "rescue check: timer is-active=$state"; bad=1; }
  next="$(systemctl show -p NextElapseUSecMonotonic --value "$name.timer" 2>/dev/null || true)"
  case "$next" in ''|0|infinity) say "rescue check: timer not due (NextElapseUSecMonotonic=${next:-none})"; bad=1 ;; esac
  [ "$bad" = 0 ] && say "${1:-rescue verified}: enabled, active, due at ${next} after boot (monotonic): networking on after $minutes min"
  return "$bad"
}

need() {
  [ -n "$nmcli_bin" ] || { say "FAIL no nmcli: networking here is not NetworkManager's; nothing changed"; exit 1; }
  [ -n "$systemctl_bin" ] || { say "FAIL no systemctl; nothing changed"; exit 1; }
}

case "$action" in
  arm)
    need
    say "networking now: $("$nmcli_bin" networking 2>&1)"
    units
    install -D -m 0644 "$tmp/service" "$svc"; say "wrote $svc (ExecStart=$nmcli_bin networking on)"
    install -D -m 0644 "$tmp/timer" "$tmr"; say "wrote $tmr (OnActiveSec=${minutes}min)"
    systemctl daemon-reload; say "systemctl daemon-reload"
    systemctl enable --quiet "$name.timer"; say "systemctl enable $name.timer (survives a reboot)"
    systemctl restart "$name.timer"; say "systemctl restart $name.timer (counts from now)"
    if verify; then echo RESCUE-VERIFIED; else say "FAIL the rescue is not verified: networking left on"; exit 1; fi ;;
  down)
    need
    verify "rescue verified again" || { say "FAIL the rescue is not armed and verified: networking left on"; exit 1; }
    cmd="$nmcli_bin networking off"
    [ "$reboot" = 1 ] && cmd="$cmd; $systemctl_bin reboot"
    unit="epitaph-offline-down-$(date +%s)"
    systemd-run --quiet --collect --unit="$unit" --on-active=5 --timer-property=AccuracySec=1s \
      /bin/sh -c "$cmd"
    say "in 5 s (transient unit $unit): $cmd"
    echo DOWN-SCHEDULED ;;
  probe)
    state="$(systemctl is-enabled "$name.timer" 2>/dev/null || true)"
    printf 'rescue=%s boot=%s\n' "${state:-none}" \
      "$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || echo '?')" ;;
  status|report)
    say "boot $(cat /proc/sys/kernel/random/boot_id 2>/dev/null || echo '?'), up $(cut -d' ' -f1 /proc/uptime 2>/dev/null || echo '?') s"
    say "networking: $( [ -n "$nmcli_bin" ] && "$nmcli_bin" networking 2>&1 || echo 'no nmcli')"
    say "rescue timer: is-enabled=$(systemctl is-enabled "$name.timer" 2>/dev/null || true) is-active=$(systemctl is-active "$name.timer" 2>/dev/null || true) files: $(ls "$svc" "$tmr" 2>/dev/null | tr '\n' ' ')"
    say "rescue journal (this boot):"
    journalctl -b --no-pager -o short-iso -n 8 -u "$name.service" 2>/dev/null | sed 's/^/    /' || true
    if [ "$action" = report ]; then
      for u in epitaph-controller epitaph-display; do
        say "$u: $(systemctl show "$u" -p ActiveState -p SubState -p Result -p NRestarts 2>/dev/null | tr '\n' ' ')"
      done
      say "time: NTPSynchronized=$(timedatectl show -p NTPSynchronized --value 2>/dev/null || echo '?') now $(date -Is)"
      say "newest lives: $(ls /var/lib/epitaph/lives 2>/dev/null | tail -n 3 | tr '\n' ' ')"
      say "controller journal (this boot, last 5):"
      journalctl -b --no-pager -o short-iso -n 5 -u epitaph-controller 2>/dev/null | sed 's/^/    /' || true
    fi ;;
  disarm)
    if [ -n "$nmcli_bin" ]; then "$nmcli_bin" networking on; say "nmcli networking on"; fi
    systemctl disable --now --quiet "$name.timer" 2>/dev/null || true
    say "systemctl disable --now $name.timer"
    rm -f "$svc" "$tmr"; say "removed $svc $tmr"
    systemctl daemon-reload; say "systemctl daemon-reload" ;;
  *) say "unknown action $action"; exit 2 ;;
esac
PI

remote() {  # remote ACTION: the Pi script as root over SSH
  local args=("$1" "$MINUTES" "$REBOOT" "$UNIT_DIR")
  if [ "$DRY" = 1 ]; then
    printf '+ %s %s sudo -n bash -s -- %s   (the Pi script, action %s)\n' "$SSH" "$HOST" "${args[*]}" "$1" >&3
    return 0
  fi
  printf '%s\n' "$PI_SCRIPT" | "$SSH" -o BatchMode=yes -o ConnectTimeout=10 "$HOST" \
    "sudo -n bash -s -- ${args[*]}"
}

HOST="$(find_host)" || { log "FAIL: the Pi does not answer (tools/pi_host.sh)"; exit 1; }

case "$ACTION" in
  status) log "status on $HOST"; remote status; exit 0 ;;
  disarm) log "disarming on $HOST"; remote disarm; exit 0 ;;
esac

# --- 1-2. arm and verify ------------------------------------------------------------------
log "arming the rescue on $HOST: networking back on after $MINUTES min"
out="$(remote arm)" || { printf '%s\n' "$out"; log "FAIL: the rescue could not be armed; networking left on"; exit 1; }
printf '%s\n' "$out" | grep -v '^RESCUE-VERIFIED$' || true
if [ "$DRY" = 0 ] && ! printf '%s\n' "$out" | grep -qx RESCUE-VERIFIED; then
  log "FAIL: the rescue is not verified; networking left on"; exit 1
fi

# --- 3. networking down ---------------------------------------------------------------------
log "taking networking down on $HOST$([ "$REBOOT" = 1 ] && echo ', then rebooting')"
out="$(remote down)" || { printf '%s\n' "$out"; log "FAIL: networking was not taken down"; exit 1; }
printf '%s\n' "$out" | grep -v '^DOWN-SCHEDULED$' || true
if [ "$DRY" = 0 ] && ! printf '%s\n' "$out" | grep -qx DOWN-SCHEDULED; then
  log "FAIL: networking was not taken down"; exit 1
fi
if [ "$DRY" = 1 ]; then log "dry run: nothing ran"; exit 0; fi
if [ "$WAIT" = 0 ]; then
  log "offline from now; networking comes back on after $MINUTES min$([ "$REBOOT" = 1 ] && echo ' of the new boot')."
  log "then: tools/offline_pi.sh --status (the Pi lock is released now: nobody can reach the Pi meanwhile)"
  exit 0
fi

# --- 4. wait for the Pi, then report ------------------------------------------------------
budget_s="${EPITAPH_OFFLINE_BUDGET_S:-$(( MINUTES * 60 + REBOOT * 600 + 600 ))}"
down_s="${EPITAPH_OFFLINE_DOWN_S:-$(( 120 + REBOOT * 60 ))}"
log "waiting up to $(( budget_s / 60 )) min for the Pi to come back"
start_s=$SECONDS
went_down=0
while :; do
  sleep "$POLL_S"
  elapsed=$(( SECONDS - start_s ))
  if probe="$(remote probe 2>/dev/null)" && [ -n "$probe" ]; then
    if [ "$went_down" = 0 ] && [ "$probe" = "${probe#rescue=enabled}" ]; then
      log "the rescue ran before the Pi was seen offline ($probe)"; break
    fi
    if [ "$went_down" = 1 ]; then log "back after ${elapsed}s ($probe)"; break; fi
    if [ "$elapsed" -ge "$down_s" ]; then
      log "FAIL: the Pi still answers ${elapsed}s after networking off ($probe); the rescue stays armed"
      exit 1
    fi
  else
    [ "$went_down" = 1 ] || log "offline: the Pi no longer answers (${elapsed}s)"
    went_down=1
    if h="$(find_host)"; then HOST="$h"; fi
  fi
  if [ "$elapsed" -ge "$budget_s" ]; then
    log "FAIL: the Pi did not come back within $(( budget_s / 60 )) min; check the screen, then power-cycle it"
    exit 1
  fi
done
remote report || true
if [ "$probe" != "${probe#rescue=enabled}" ]; then
  log "FAIL: the Pi came back before the rescue ran (did NetworkManager keep 'networking off'"
  log "across the reboot?); this was not an offline run. The rescue is still armed: --disarm"
  exit 1
fi
log "PASS: offline for about ${elapsed}s, back by the rescue"
exit 0
