#!/usr/bin/env bash
# Start a spike script on the Pi as a transient unit and wait for it (called by pi_run.sh
# under the Pi lock). SSH failures while polling are retried: the unit keeps running.
host="$1"; unit="$2"; script="$3"; shift 3
args="$(printf '%q ' "$@")"
ssh "$host" "sudo -n systemctl reset-failed $unit 2>/dev/null; sudo -n systemd-run --quiet --unit=$unit --uid=pi --gid=pi --setenv=HOME=/home/pi --working-directory=/home/pi/epitaph-spike python3 -u tools/spike/$script $args" || exit 1
sleep 5
fails=0
while :; do
  st="$(ssh -o ConnectTimeout=15 "$host" "systemctl is-active $unit")"; rc=$?
  if [ "$st" = active ] || [ "$st" = activating ]; then fails=0
  elif [ "$rc" -eq 255 ] && [ "$fails" -lt 40 ]; then fails=$((fails + 1))
  else break
  fi
  sleep 15
done
ssh "$host" "journalctl -u $unit -o cat --no-pager | grep -v -E '^(Started|Finished|$unit|pam_unix| *pi :)' | tail -40; systemctl show $unit -p Result"
