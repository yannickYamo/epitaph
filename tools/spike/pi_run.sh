#!/usr/bin/env bash
# Run one spike script on the Pi as a detached unit, under the Pi lock, and copy results back.
#   tools/spike/pi_run.sh <minutes> <name> <script.py> [args...]
# Output paths in args are relative to ~/epitaph-spike on the Pi (use bench/...); bench/ is
# rsynced back into this worktree afterwards. The unit survives an SSH drop (PI_FACTS lessons).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"; root="$(cd "$here/../.." && pwd)"
minutes="$1"; name="$2"; script="$3"; shift 3
unit="spike-a-$name"
ssh pi 'mkdir -p ~/epitaph-spike/tools/spike ~/epitaph-spike/config ~/epitaph-spike/bench/spike'
rsync -a "$here/"*.py pi:epitaph-spike/tools/spike/
rsync -a "$root/config/default.toml" pi:epitaph-spike/config/
args="$(printf '%q ' "$@")"
"$root/tools/pi_lock.sh" run A "$minutes" -- bash -c "
  ssh pi 'sudo -n systemctl reset-failed $unit 2>/dev/null; sudo -n systemd-run --quiet --unit=$unit --uid=pi --gid=pi --setenv=HOME=/home/pi --working-directory=/home/pi/epitaph-spike python3 -u tools/spike/$script $args'
  sleep 5
  while ssh -o ConnectTimeout=15 pi 'systemctl is-active --quiet $unit'; do sleep 15; done
  ssh pi 'journalctl -u $unit -o cat --no-pager | grep -v -E \"^(Started|Finished|$unit)\" | tail -40; systemctl show $unit -p Result'
"
rsync -a pi:epitaph-spike/bench/ "$root/bench/"
