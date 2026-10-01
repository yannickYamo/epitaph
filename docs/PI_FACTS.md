# Pi facts (after step 0, 2026-09-29)

| Item | Value |
|---|---|
| Board | Raspberry Pi 4 Model B Rev 1.5, 4 GB, 4 × Cortex-A72 1.8 GHz, bootloader 2022-04-26 |
| OS | Raspberry Pi OS 64-bit (Debian 13 trixie, image 2026-09-15), kernel 6.18.50+rpt-rpi-v8, Python 3.13.5, console boot |
| Hostname / access | `epitaph`; laptop aliases `pi` = `epitaph.local` (Wi-Fi, key only), `pi-eth` = 10.42.0.95 (cable; passwords allowed if one is set). Tools try `pi`, then `pi-eth` (see "Reaching the Pi") |
| Network | Wi-Fi `<home-wifi>` (US regdomain, powersave off) carries the default route (metric 600); the cable is the maintenance link and the route of last resort (metric 800, through the laptop's shared connection) |
| Swap | zram only: 2 GB compressed RAM (zstd), swappiness 10, no swap file on the card (`/etc/rpi/swap.conf.d/90-epitaph.conf`, `/etc/sysctl.d/90-epitaph.conf`). The creature's cgroup has `memory.swap.max = 0` |
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
| CPU frequency | One cpufreq policy (`policy0`) for cores 0-3, governor `ondemand`, steps 600-1800 MHz by 100; `scaling_max_freq` is root-only (hence the clock helper below) |

## The installed service (phase 1, `deploy/install.sh`)

`tools/pi_deploy.sh` (under the Pi lock) rsyncs the working tree to `/opt/epitaph/src` and
runs `sudo deploy/install.sh` there. A second install changes nothing (`changed: 0`).

| Item | Value |
|---|---|
| Service user | `pi`. The Pi is dedicated to the piece; `/var/lib/epitaph`, the models and the llama.cpp build in `~/llama.cpp` already belong to `pi`. A separate user would need its own copy of, or permissions on, all three for no gain on a single-purpose machine. `install.sh --user NAME` installs for another user |
| Source | `/opt/epitaph/src`, owned by `pi` |
| Python | `/opt/epitaph/venv` (owned by `pi`): the package installed editable from the source, with the `display` extra, so `config/` and `bench/` sit next to the code and a deploy takes effect at the next start. Rebuilt only when `pyproject.toml`, the extras or Python change |
| State | `/var/lib/epitaph` (lives, counter, status); models in `/var/lib/epitaph/models/<model>/<quant>.gguf` |
| Units | `epitaph-controller.service` (Type=notify, WatchdogSec=30, Delegate=yes, CPUAffinity=0, Restart=always, OOMPolicy=continue, runs `epitaph run`) and `epitaph-display.service` (`ExecCondition=epitaph display --screen-present`: headless, the unit is skipped, not failed), in `/etc/systemd/system`. Installed **disabled**; `install.sh --enable` enables both at boot |
| CPU clock (ADR-025) | `/usr/local/sbin/epitaph-clock <MHz>` (600-1800) or `reset`: root-owned, writes `scaling_max_freq` on every policy, accepts nothing else. `/etc/sudoers.d/020_epitaph-clock` lets the service user run exactly that without a password (the rule only matches `reset` or a 3-4 digit number). The unit resets the clock before every start and after every stop; the body resets it at every death and at every start |
| Watchdogs | The controller pings systemd (WATCHDOG=1) every 15 s through `epitaph.body.watchdog.Notifier`; below it the hardware watchdog (`RuntimeWatchdogSec=1m`, OS default), which `install.sh` checks |
| Selftest | `epitaph selftest [--user pi]` relaunches itself as a transient `Delegate=yes` unit for the service user (`sudo systemd-run --uid … --pipe --wait --collect`) and checks the controllers, the leaves, limits set and cleared, `cgroup.kill`, the progress counters, the clock round trip and the llama-server binary; exit 0 or 1. `install.sh` runs it last |

## Reaching the Pi

- **Two aliases, one order.** `tools/pi_host.sh` tries `pi` (Wi-Fi, mDNS) and then `pi-eth`
  (the cable); every tool that talks to the Pi uses it (`PI_HOST` pins one alias). The remote
  view does the same: `epitaph display --connect pi` means `pi,pi-eth`.
- **Why `epitaph.local` fails.** On 2026-09-30 the cause was the Pi, not mDNS: its saved Wi-Fi
  network was out of range, so `wlan0` never came up and avahi (which publishes on `wlan0`
  only) had nothing to announce. Avahi itself logged no conflicts; its settings were left as
  they are. When the Pi moves, a person adds the new network on the Pi (`ssh -t pi-eth sudo
  nmcli --ask device wifi connect "<SSID>"`, in your own terminal) or the Pi stays on the cable.
- **Plain `ssh pi` with the same fallback** (optional, your own `~/.ssh/config`; put it above
  `Host pi`, because the first `HostName` wins):

  ```
  Match originalhost pi exec "! timeout 3 getent hosts epitaph.local >/dev/null"
      HostName 10.42.0.95
  ```

  It costs 3 s per connection while mDNS fails.
- **Address reservations (the owner's option).** A reservation removes the guesswork:
  - Wi-Fi: reserve the Pi's Wi-Fi MAC in the router's DHCP settings, then point `pi` at that
    address in `~/.ssh/config` (keep `epitaph.local` as a comment). Nothing on the Pi changes.
  - Cable: the laptop's shared connection hands out 10.42.0.x through its own dnsmasq, which
    in practice gives the same Pi the same address. To pin it, on the laptop:
    `echo 'dhcp-host=<pi-eth0-mac>,10.42.0.95' | sudo tee /etc/NetworkManager/dnsmasq-shared.d/epitaph.conf`
    and reconnect the cable. A static address on the Pi is not needed.
- **No Wi-Fi, no clock.** The Pi has no RTC. Without Wi-Fi it reaches NTP only through the
  cable, which needs the laptop sharing its connection. `pi_bootstrap.sh` reports
  `clock-synced` so a slow clock is seen before a life or a benchmark relies on it.

## Lessons from step 0 (read before touching the Pi)

- **Pi OS ships drop-ins that override yours** (`/usr/lib/systemd/system.conf.d/40-rpi-enable-watchdog.conf`, `/usr/lib/systemd/journald.conf.d/40-rpi-volatile-storage.conf`). Name your drop-ins `90-…` and verify the effective value (`systemd-analyze cat-config`, `systemctl show`) after every change.
- **Long jobs must not hang off an SSH session.** Wi-Fi SSH can drop; run long work as a detached unit (`sudo systemd-run --unit=<name> --uid=pi --gid=pi --setenv=HOME=/home/pi …`) and poll `systemctl is-active <name>`.
- **`sync` after writing important files.** A crash before sync left a zero-length config file.
- **Never put a secret in a sudo argument:** sudo logs its command line to the journal. Pass secrets on stdin.
- **Power:** the official 5.1 V / 3 A supply is required; with it, 4 cores at full load showed no under-voltage (`vcgencmd get_throttled` = 0x0). Log `get_throttled` in every load test.
- Paths on the Pi: llama.cpp at `~/llama.cpp` (tag b11277, binaries in `build/bin`); state in `/var/lib/epitaph` (create with sudo, owned by `pi`); models in `/var/lib/epitaph/models/<model>/<quant>.gguf`.
- Copy large files over the cable (`rsync … pi-eth:`), not Wi-Fi.
