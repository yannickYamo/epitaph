# Pi changes

Every system-level change to the Pi, in order. Commands and results only, never secrets (BUILD_PLAN 0.10, 8.6).

| When | Who | Change | Result |
|---|---|---|---|
| 2026-09-29 | L | Raspberry Pi OS Desktop 64-bit (trixie, 2026-09-15) flashed; cloud-init user `pi`, SSH key from the laptop installed | Boots; reachable at 10.42.0.95 over the cable |
| 2026-09-29 | L | Laptop: `apt install tesseract-ocr qemu-user-static podman` (step 0.1) | tesseract 5.3.4, podman 4.9.3, qemu-aarch64-static present |
| 2026-09-29 | L | `/etc/sudoers.d/010_pi-nopasswd` (`pi ALL=(ALL) NOPASSWD: ALL`, mode 0440, validated with `visudo -cf`) (step 0.3) | `sudo -n true` OK |
| 2026-09-29 | L | Scratch file holding the initial Pi password shredded on the laptop (step 0.4) | Deleted; Yannick sets a new password himself |
| 2026-09-29 | L | `fstrim -v /` (part of `tools/sd_backup.sh`, step 0.2) | 333.5 MiB trimmed |
| 2026-09-29 | L | SD backup `~/epitaph-backups/step0-20260929-2124` (sfdisk + p1 dd + p2 `e2image -raf`, zstd, sha256); restore tested into a loop file: journal replayed, e2fsck rc=1 (counters fixed) | 3.9 GB; restorable |
| 2026-09-29 | L | Wi-Fi country US (`raspi-config nonint do_wifi_country US`, `rfkill unblock wifi`); added `cfg80211.ieee80211_regdom=US` to cmdline | wlan0 available |
| 2026-09-29 | L | Wi-Fi system connection `<home-wifi>` (`nmcli --ask`, secret piped on stdin), `802-11-wireless.powersave 2`, `autoconnect-priority 10` | <pi-wifi-ip>; secret only in `/etc/NetworkManager/system-connections/<home-wifi>.nmconnection` (600 root) |
| 2026-09-29 | L | Journal rotated and vacuumed twice: an audit `sudo grep` had put the Wi-Fi secret on sudo's logged command line (my mistake, fixed; audits now pass patterns on stdin) | 0 hits in journal and /var/log |
| 2026-09-29 | L | `netplan-eth0`: `ipv4.never-default yes`, `ipv6.never-default yes`; reapplied | Default route via wlan0 only; cable = 10.42.0.0/24 maintenance link |
| 2026-09-29 | L | `touch /etc/cloud/cloud-init.disabled`; hostname `epitaph` (`hostnamectl`, `/etc/hosts` rewritten); password hash in `/boot/firmware/user-data` replaced with `REMOVED-after-first-boot` (copy in /root) | Hostname survives reboot |
| 2026-09-29 | L | `passwd -l pi`: the first-boot password appeared in a chat transcript. Yannick can set a new one: `ssh -t pi sudo passwd pi` | Password login impossible until then; keys work |
| 2026-09-29 | L | avahi `allow-interfaces=wlan0` | `epitaph.local` = Wi-Fi address |
| 2026-09-29 | L | `cmdline.txt` (backup `cmdline.txt.bak-step0`): appended `cgroup_enable=memory cgroup_memory=1 consoleblank=0` | After reboot: controllers `cpuset cpu io memory pids`; consoleblank 0 |
| 2026-09-29 | L | `systemctl set-default multi-user.target` | Console boot; available RAM 3460 → 3593 MB (+133 MB, less than the plan's 350 MB estimate) |
| 2026-09-29 | L | Watchdog: added then removed a `RuntimeWatchdogSec=15` drop-in; the OS already ships `40-rpi-enable-watchdog.conf` (1 min) and the hardware accepts it | `RuntimeWatchdogUSec=1min` |
| 2026-09-29 | L | sshd: `/etc/ssh/sshd_config.d/10-epitaph.conf` (no passwords, no root; `Match Address 10.42.0.0/24` allows passwords); cloud-init's `50-cloud-init.conf` moved to /root | Wi-Fi offers `publickey` only; cable offers `publickey,password` |
| 2026-09-29 | L | Final S0 check with eth0 down: sudo, memory cgroup, console target, hostname, internet, DNS, NTP over Wi-Fi | All pass; cable restored |
| 2026-09-29 | L | **Power finding:** the Pi rebooted about 2 min into the 4-core llama.cpp build, and again within seconds of a plain 3-core busy loop. `vcgencmd get_throttled` = 0x50000 (under-voltage and throttling occurred); dmesg "Undervoltage detected!". A config file written just before the crash came back empty (not synced) | **Blocker:** Pi work paused until the official 5.1 V / 3 A USB-C supply is in use |
| 2026-09-29 | L | Timezone America/Los_Angeles (was Europe/London) | Exhibition hours use local time |
| 2026-09-29 | L | Persistent journal: `/etc/systemd/journald.conf.d/90-epitaph.conf` (`Storage=persistent`, 200 MB); `90-` so it sorts after Pi OS's `40-rpi-volatile-storage.conf` | Crash evidence now survives reboots |
| 2026-09-29 | C | `tools/pi_bootstrap.sh` first runs. A false sshd failure (pipefail + `grep -q`, fixed) made it rewrite `10-epitaph.conf` twice with identical content and `systemctl reload ssh`; the two backups it left in `sshd_config.d/` were moved to /root | Content unchanged (sha256 same); sshd fine |
| 2026-09-29 | C | Bootstrap idempotence proof: `--check`, `--state`, `--apply`, `--state`, `--apply`, `--state` | 0 changed, 0 drift, no reboot; the three state dumps identical |
| 2026-09-29 | C | Bootstrap drift test: timezone set to UTC and `90-epitaph.conf` (journald) deleted by hand, then `--apply` | Both reported and restored (`changed: 2`); effective state equal to before; journald restarted |
