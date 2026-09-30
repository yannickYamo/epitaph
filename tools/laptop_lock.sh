#!/usr/bin/env bash
# One llama-server job at a time on the laptop (about 9 GB RAM free). See lock.sh.
exec "$(dirname "$0")/lock.sh" laptop "$@"
