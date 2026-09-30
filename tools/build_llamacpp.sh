#!/usr/bin/env bash
# Build llama.cpp at the pinned tag (BUILD_PLAN 9 A1). Runs on the machine it is built for.
#   tools/build_llamacpp.sh [tag] [prefix]      default tag from config/models.toml, prefix ~/llama.cpp
# On the Pi, run it remotely under the lock:
#   tools/pi_lock.sh run A 90 -- ssh pi 'bash -s' < tools/build_llamacpp.sh
set -euo pipefail

TAG="${1:-${LLAMACPP_TAG:-b11277}}"
PREFIX="${2:-$HOME/llama.cpp}"
JOBS="$(nproc)"

need=(build-essential cmake git)
missing=()
for p in "${need[@]}"; do dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p"); done
if [ "${#missing[@]}" -gt 0 ]; then
  sudo -n apt-get update -qq
  sudo -n DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}"
fi

if [ -d "$PREFIX/.git" ]; then
  git -C "$PREFIX" fetch -q --depth 1 origin "refs/tags/$TAG:refs/tags/$TAG"
  git -C "$PREFIX" checkout -q "$TAG"
else
  git clone -q --depth 1 --branch "$TAG" https://github.com/ggml-org/llama.cpp "$PREFIX"
fi

arch="$(uname -m)"
flags=(-DCMAKE_BUILD_TYPE=Release -DLLAMA_CURL=OFF -DLLAMA_BUILD_TESTS=OFF -DGGML_NATIVE=ON)
[ "$arch" = "x86_64" ] && [ "${VULKAN:-0}" = "1" ] && flags+=(-DGGML_VULKAN=ON)

cmake -S "$PREFIX" -B "$PREFIX/build" "${flags[@]}" >/dev/null
cmake --build "$PREFIX/build" -j "$JOBS" --target llama-server llama-bench llama-quantize llama-cli 2>&1 | tail -3

"$PREFIX/build/bin/llama-server" --version 2>&1 | head -2
echo "built llama.cpp $TAG on $arch in $PREFIX"
