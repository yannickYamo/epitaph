#!/usr/bin/env bash
# Run one spike script on the Pi as a detached unit, under the Pi lock, and copy results back.
#   tools/spike/pi_run.sh <minutes> <name> <script.py> [args...]
# Output paths in args are relative to ~/epitaph-spike on the Pi (use bench/...); bench/ is
# rsynced back into this worktree afterwards. The unit survives an SSH drop (PI_FACTS lessons).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"; root="$(cd "$here/../.." && pwd)"
minutes="$1"; name="$2"; script="$3"; shift 3
unit="spike-a-$name"
# `pi` goes over Wi-Fi via mDNS (epitaph.local); when it does not answer, the cable (F12).
# shellcheck source=tools/pi_host.sh
. "$root/tools/pi_host.sh"
host="$(PI_HOST="${PI_SSH:-${PI_HOST:-}}" pi_host)"
ssh $host 'mkdir -p ~/epitaph-spike/tools/spike ~/epitaph-spike/config ~/epitaph-spike/bench/spike'
rsync -a "$here/"*.py $host:epitaph-spike/tools/spike/
rsync -a "$root/config/default.toml" $host:epitaph-spike/config/
"$root/tools/pi_lock.sh" run A "$minutes" -- bash "$here/_pi_unit.sh" "$host" "$unit" "$script" "$@"
# Measured costs wait in bench/measured/ until the integrator rebases the profiles on them
# (costmodel.load_costs reads bench/*.json directly; see docs/SPIKE.md).
ssh $host 'mkdir -p ~/epitaph-spike/bench/measured; for f in ~/epitaph-spike/bench/pi4-*.json; do [ -e "$f" ] && mv "$f" ~/epitaph-spike/bench/measured/; done; true'
rsync -a $host:epitaph-spike/bench/ "$root/bench/"
