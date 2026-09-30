#!/usr/bin/env bash
# Pick the SSH alias that reaches the Pi right now (BUILD_PLAN F12).
#
#   tools/pi_host.sh              print the first alias that answers, exit 1 if none does
#   . tools/pi_host.sh; pi_host   the same as a function, for the other tools
#
# The laptop knows the Pi by two aliases in ~/.ssh/config (docs/PI_FACTS.md):
#   pi      epitaph.local over Wi-Fi (mDNS; keys only)
#   pi-eth  the direct cable, NetworkManager's shared 10.42.0.x subnet
# mDNS fails whenever the Pi is off Wi-Fi (for example in a room where its network is out of
# range) and sometimes on the laptop side, so every tool tries `pi` first and falls back to
# the cable. PI_HOST set by the caller wins and is not probed; PI_HOSTS changes the order.
#
# The probe is one `ssh <alias> true` per alias (a few ms on the Pi), bounded by `timeout`
# because name resolution is not covered by ConnectTimeout.

pi_host() {
  if [ -n "${PI_HOST:-}" ]; then printf '%s\n' "$PI_HOST"; return 0; fi
  local h
  for h in ${PI_HOSTS:-pi pi-eth}; do
    if timeout 20 ssh -o BatchMode=yes -o ConnectTimeout=6 "$h" true 2>/dev/null; then
      printf '%s\n' "$h"; return 0
    fi
    echo "[pi_host] $h does not answer; trying the next alias" >&2
  done
  echo "[pi_host] the Pi answers on none of: ${PI_HOSTS:-pi pi-eth} (see docs/PI_FACTS.md)" >&2
  return 1
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  set -euo pipefail
  pi_host
fi
