#!/usr/bin/env bash
# Build llama.cpp at the pinned tag. Runs on the machine it is built for.
#   tools/build_llamacpp.sh [tag] [prefix]      default tag from config/models.toml, prefix ~/llama.cpp
#   tools/build_llamacpp.sh --pi [tag]          build on the Pi as a detached unit, under the Pi lock
#
# On the Pi the build (about 1 h at -j4) runs as the systemd unit `llama-build`, so an SSH drop
# cannot kill it (PI_FACTS "Lessons from step 0"). A power loss mid-build leaves zero-length
# object files that make the link fail with "undefined reference to ggml_backend_*"; the build
# deletes any empty or pre-crash objects first.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"

tag_from_config() {
  sed -n 's/^llamacpp_tag *= *"\(.*\)"/\1/p' "$here/../config/models.toml" 2>/dev/null | head -1
}

if [ "${1:-}" = "--pi" ]; then
  # Take the Pi lock first, then do everything (host probe, copy, build, poll) inside it.
  shift; exec "$here/pi_lock.sh" run "${HOLDER:-build}" 120 -- "$0" --pi-locked "$@"
fi
if [ "${1:-}" = "--pi-locked" ]; then
  TAG="${2:-$(tag_from_config)}"; TAG="${TAG:-b11277}"
  # shellcheck source=tools/pi_host.sh
  . "$here/pi_host.sh"; host="$(pi_host)" || exit 3
  scp -q "$0" "$host:/tmp/build_llamacpp.sh"
  ssh "$host" "sudo -n systemctl reset-failed llama-build 2>/dev/null; sudo -n systemd-run --unit=llama-build --uid=pi --gid=pi --setenv=HOME=/home/pi --working-directory=/home/pi bash /tmp/build_llamacpp.sh $TAG"
  while ssh -o ConnectTimeout=10 "$host" 'systemctl is-active --quiet llama-build'; do sleep 30; done
  exec ssh "$host" 'systemctl show llama-build -p Result; tail -5 ~/llama.cpp/build.log; vcgencmd get_throttled'
fi

TAG="${1:-${LLAMACPP_TAG:-$(tag_from_config)}}"; TAG="${TAG:-b11277}"
PREFIX="${2:-$HOME/llama.cpp}"
JOBS="$(nproc)"
LOG="$PREFIX/build.log"

need=(build-essential cmake git)
missing=()
for p in "${need[@]}"; do dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p"); done
if [ "${#missing[@]}" -gt 0 ]; then
  sudo -n apt-get update -qq
  sudo -n DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}"
fi

if [ -d "$PREFIX/.git" ]; then
  if [ "$(git -C "$PREFIX" describe --tags --exact-match 2>/dev/null)" != "$TAG" ]; then
    git -C "$PREFIX" fetch -q --depth 1 origin "refs/tags/$TAG:refs/tags/$TAG"
    git -C "$PREFIX" checkout -q "$TAG"
  fi
else
  git clone -q --depth 1 --branch "$TAG" https://github.com/ggml-org/llama.cpp "$PREFIX"
fi

# Objects left empty or half-written by a crash break the link; drop them (and anything older
# than this boot on machines that crashed) so make rebuilds them.
if [ -d "$PREFIX/build" ]; then
  find "$PREFIX/build" -name '*.o' -size 0 -print -delete
  if [ -r /proc/uptime ] && [ "${KEEP_OLD_OBJECTS:-0}" != "1" ]; then
    boot="$(uptime -s)"
    find "$PREFIX/build" -name '*.o' ! -newermt "$boot" -print -delete
  fi
fi

arch="$(uname -m)"
flags=(-DCMAKE_BUILD_TYPE=Release -DLLAMA_CURL=OFF -DLLAMA_BUILD_TESTS=OFF -DGGML_NATIVE=ON)
[ "$arch" = "x86_64" ] && [ "${VULKAN:-0}" = "1" ] && flags+=(-DGGML_VULKAN=ON)

{
  echo "== $(date -Is) build $TAG on $arch, -j$JOBS"
  cmake -S "$PREFIX" -B "$PREFIX/build" "${flags[@]}"
  cmake --build "$PREFIX/build" -j "$JOBS" --target llama-server llama-bench llama-quantize llama-cli
} >"$LOG" 2>&1 || { grep -E 'error|Error' "$LOG" | head -20; tail -5 "$LOG"; exit 2; }
sync

for b in llama-server llama-bench; do
  [ -x "$PREFIX/build/bin/$b" ] || { echo "missing $b" >&2; exit 3; }
done
echo "tag: $(git -C "$PREFIX" describe --tags) commit $(git -C "$PREFIX" rev-parse --short HEAD)"
"$PREFIX/build/bin/llama-server" --version 2>&1 | tail -2
echo "built llama.cpp $TAG on $arch in $PREFIX" | tee -a "$LOG"
