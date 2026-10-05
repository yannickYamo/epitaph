#!/usr/bin/env bash
# Restore a backup made by sd_backup.sh onto a card or a loop file.
# DESTROYS everything on the target. Usage: tools/sd_restore.sh <backup_dir> <target_device_or_file>
set -euo pipefail

SRC="${1:?backup dir}"
DST="${2:?target device or image file}"

( cd "$SRC" && sha256sum -c p1-boot.sha256 p2-root.sha256 )

if [ -b "$DST" ]; then
  echo "About to overwrite block device $DST:"
  lsblk "$DST"
  read -r -p "Type the device path again to confirm: " ok
  [ "$ok" = "$DST" ] || { echo "aborted"; exit 1; }
  sudo sfdisk "$DST" < "$SRC/partitions.sfdisk"
  sudo partprobe "$DST"; sleep 2
  P1=$(lsblk -lnpo NAME "$DST" | sed -n 2p); P2=$(lsblk -lnpo NAME "$DST" | sed -n 3p)
  zstd -dc "$SRC/p1-boot.img.zst" | sudo dd of="$P1" bs=4M conv=fsync status=none
  zstd -dc "$SRC/p2-root.img.zst" | sudo dd of="$P2" bs=4M conv=sparse,fsync status=none
else
  # Loop-file restore, used to test backups without a spare card.
  # Size the file to the end of the last partition (DOS tables have no last-lba).
  end=$(sed -nE 's/.*start= *([0-9]+), size= *([0-9]+).*/\1 \2/p' "$SRC/partitions.sfdisk" \
        | awk '{e=$1+$2; if (e>m) m=e} END {print m}')
  [ -n "$end" ] || { echo "cannot read partition table" >&2; exit 1; }
  rm -f "$DST"
  truncate -s $(( (end + 2048) * 512 )) "$DST"
  sfdisk -q "$DST" < "$SRC/partitions.sfdisk"
  LOOP=$(sudo losetup -Pf --show "$DST")
  zstd -dc "$SRC/p1-boot.img.zst" | sudo dd of="${LOOP}p1" bs=4M status=none
  zstd -dc "$SRC/p2-root.img.zst" | sudo dd of="${LOOP}p2" bs=4M conv=sparse status=none
  # The image was taken live, so replay the journal and repair the copy as the Pi would
  # at boot (fsck.repair=yes). Exit codes 0-1 mean clean or fixed.
  set +e; sudo e2fsck -fy "${LOOP}p2" >/tmp/sd_restore_fsck.log 2>&1; fsck_rc=$?; set -e
  tail -3 /tmp/sd_restore_fsck.log
  if [ "$fsck_rc" -le 1 ]; then echo "rootfs check OK (e2fsck rc=$fsck_rc)"; else echo "rootfs check FAILED (e2fsck rc=$fsck_rc)" >&2; sudo losetup -d "$LOOP"; exit 1; fi
  sudo losetup -d "$LOOP"
fi
sync
echo "restored $SRC -> $DST"
