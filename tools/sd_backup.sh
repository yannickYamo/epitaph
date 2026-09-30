#!/usr/bin/env bash
# Partition-aware backup of the Pi's SD card to the laptop (BUILD_PLAN 8.6 step 2).
# Saves the partition table, the boot partition, and the used blocks of the rootfs.
# The rootfs is mounted while imaged, so the result is crash-consistent.
# Usage: tools/sd_backup.sh [host] [out_dir]
# Without a host it prefers the cable (`pi-eth`, several GB) and falls back to Wi-Fi.
set -euo pipefail

# shellcheck source=tools/pi_host.sh
. "$(dirname "$0")/pi_host.sh"
HOST="${1:-$(PI_HOSTS="${PI_HOSTS:-pi-eth pi}" pi_host)}"
OUT="${2:-$HOME/epitaph-backups/$(date +%Y%m%d-%H%M%S)}"
DISK=/dev/mmcblk0

mkdir -p "$OUT"
echo "backup of $HOST:$DISK -> $OUT"

ssh -o BatchMode=yes "$HOST" 'sudo -n /usr/sbin/fstrim -v /'

ssh -o BatchMode=yes "$HOST" "sudo -n /usr/sbin/sfdisk -d $DISK" > "$OUT/partitions.sfdisk"

ssh -o BatchMode=yes "$HOST" "set -o pipefail; sudo -n dd if=${DISK}p1 bs=4M status=none | zstd -q -T3 -3" \
  | tee "$OUT/p1-boot.img.zst" | sha256sum | sed 's|-|p1-boot.img.zst|' > "$OUT/p1-boot.sha256"

# e2image -ra writes a raw image with only the used blocks read; the unused space
# comes out as zeros, which zstd compresses to almost nothing.
ssh -o BatchMode=yes "$HOST" "set -o pipefail; sudo -n /usr/sbin/e2image -raf ${DISK}p2 - | zstd -q -T3 -1" \
  | tee "$OUT/p2-root.img.zst" | sha256sum | sed 's|-|p2-root.img.zst|' > "$OUT/p2-root.sha256"

for f in p1-boot.img.zst p2-root.img.zst; do
  [ "$(stat -c %s "$OUT/$f")" -gt 10000000 ] || { echo "ERROR: $f is suspiciously small; backup failed" >&2; exit 1; }
done

ssh -o BatchMode=yes "$HOST" 'cat /proc/device-tree/model; echo; uname -r; date -Is' > "$OUT/source.txt"
ls -lh "$OUT"
echo "done: $OUT"
