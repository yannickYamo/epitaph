# Pi facts (after step 0, 2026-09-29)

| Item | Value |
|---|---|
| Board | Raspberry Pi 4 Model B Rev 1.5, 4 GB, 4 × Cortex-A72 1.8 GHz, bootloader 2022-04-26 |
| OS | Raspberry Pi OS 64-bit (Debian 13 trixie, image 2026-09-15), kernel 6.18.50+rpt-rpi-v8, Python 3.13.5, console boot |
| Hostname / access | `epitaph`; laptop aliases `pi` = `epitaph.local` (Wi-Fi, key only), `pi-eth` = 10.42.0.95 (cable; passwords allowed if one is set) |
| Network | Wi-Fi `<home-wifi>` (US regdomain, powersave off) carries the default route; the cable is maintenance-only (`never-default`) |
| Storage | 2017 SanDisk 64 GB (SP64G); p1 512 MB vfat, p2 59 GB ext4; about 49 GB free |
| cgroups | v2, controllers `cpuset cpu io memory pids` (memory enabled via cmdline; firmware injects `cgroup_disable=memory`) |
| RAM | 3.7 GiB total; about 3.59 GB available at idle on console boot |
| Watchdog | bcm2835, `RuntimeWatchdogSec=1m` (OS default) |
| Clock | NTP over Wi-Fi (no RTC) |
| sudo | Passwordless for `pi` (`/etc/sudoers.d/010_pi-nopasswd`) |
| Password | `pi` password locked until Yannick sets one |
| cloud-init | Disabled after first boot |
| Screen | None connected |
| Hardware class | `pi4` (overlay `pi4-4gb`) |
| Backup | `~/epitaph-backups/step0-20260929-2124` on the laptop (restore tested) |
