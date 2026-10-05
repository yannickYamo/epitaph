#!/usr/bin/env bash
# One llama-server job at a time on the laptop. See lock.sh.
exec "$(dirname "$0")/lock.sh" laptop "$@"
