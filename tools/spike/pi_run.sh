#!/usr/bin/env bash
# Run one spike script on the Pi as a detached unit, under the Pi lock, and copy results back.
#   tools/spike/pi_run.sh <minutes> <name> <script.py> [args...]
# Output paths in args are relative to ~/epitaph-spike on the Pi (use bench/...); bench/ is
# rsynced back into this worktree afterwards. The unit survives an SSH drop (PI_FACTS lessons).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"; root="$(cd "$here/../.." && pwd)"
minutes="$1"; name="$2"; script="$3"; shift 3
unit="spike-a-$name"
# `pi` goes over Wi-Fi via mDNS (epitaph.local); when the laptop cannot resolve it, use the cable.
host="${PI_SSH:-pi}"
ssh -o ConnectTimeout=8 "$host" true 2>/dev/null || host=pi-eth
ssh $host 'mkdir -p ~/epitaph-spike/tools/spike ~/epitaph-spike/config ~/epitaph-spike/bench/spike'
rsync -a "$here/"*.py $host:epitaph-spike/tools/spike/
rsync -a "$root/config/default.toml" $host:epitaph-spike/config/
"$root/tools/pi_lock.sh" run A "$minutes" -- bash "$here/_pi_unit.sh" "$host" "$unit" "$script" "$@"
# Measured costs wait in bench/measured/ until the integrator rebases the profiles on them
# (costmodel.load_costs reads bench/*.json directly; see docs/SPIKE.md).
ssh $host 'mkdir -p ~/epitaph-spike/bench/measured; for f in ~/epitaph-spike/bench/pi4-*.json; do [ -e "$f" ] && mv "$f" ~/epitaph-spike/bench/measured/; done; true'
# --update: a file edited here since (e.g. a note added to a parked result) is newer and kept.
rsync -a --update $host:epitaph-spike/bench/ "$root/bench/"
