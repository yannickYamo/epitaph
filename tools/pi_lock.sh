#!/usr/bin/env bash
# Serialises all Pi work (deploys, reboots, benches, spikes, lives). See lock.sh.
exec "$(dirname "$0")/lock.sh" pi "$@"
