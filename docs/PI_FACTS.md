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

## Lessons from step 0 (read before touching the Pi)

- **Pi OS ships drop-ins that override yours** (`/usr/lib/systemd/system.conf.d/40-rpi-enable-watchdog.conf`, `/usr/lib/systemd/journald.conf.d/40-rpi-volatile-storage.conf`). Name your drop-ins `90-…` and verify the effective value (`systemd-analyze cat-config`, `systemctl show`) after every change.
- **Long jobs must not hang off an SSH session.** Wi-Fi SSH can drop; run long work as a detached unit (`sudo systemd-run --unit=<name> --uid=pi --gid=pi --setenv=HOME=/home/pi …`) and poll `systemctl is-active <name>`.
- **`sync` after writing important files.** A crash before sync left a zero-length config file.
- **Never put a secret in a sudo argument:** sudo logs its command line to the journal. Pass secrets on stdin.
- **Power:** the official 5.1 V / 3 A supply is required; with it, 4 cores at full load showed no under-voltage (`vcgencmd get_throttled` = 0x0). Log `get_throttled` in every load test.
- Paths on the Pi: llama.cpp at `~/llama.cpp` (tag b11277, binaries in `build/bin`); state in `/var/lib/epitaph` (create with sudo, owned by `pi`); models in `/var/lib/epitaph/models/<model>/<quant>.gguf`.
- Copy large files over the cable (`rsync … pi-eth:`), not Wi-Fi.
