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
| Clock | NTP over Wi-Fi (no RTC). Offline, each boot restores the last saved time (see "Offline operation") |
| sudo | Passwordless for `pi` (`/etc/sudoers.d/010_pi-nopasswd`) |
| Password | `pi` password locked until Yannick sets one |
| cloud-init | Disabled after first boot |
| Screen | None connected |
| Hardware class | `pi4` (overlay `pi4-4gb`) |
| Backup | `~/epitaph-backups/step0-20260929-2124` on the laptop (restore tested) |
| CPU frequency | One cpufreq policy (`policy0`) for cores 0-3, governor `ondemand`, steps 600-1800 MHz by 100; `scaling_max_freq` is root-only (hence the clock helper below) |

## The installed service (phase 1, `deploy/install.sh`)

`tools/pi_deploy.sh` (under the Pi lock) rsyncs the working tree to `/opt/epitaph/src` and
runs `sudo deploy/install.sh` there. A second install changes nothing (`changed: 0`). The
same script installs from a fresh Raspberry Pi OS ([INSTALLATION.md](INSTALLATION.md)): it adds
`python3-venv`, `nftables` and `sudo` when missing, and refuses a source anywhere but
`/opt/epitaph/src`. `make install-test-arm64` proves it in a clean arm64 Debian trixie container
under qemu (`install.sh --container`: no booted systemd, so no daemon-reload, cgroup, watchdog
or selftest steps); about 10-12 minutes, outside CI.

| Item | Value |
|---|---|
| Service user | `pi`. The Pi is dedicated to the piece; `/var/lib/epitaph`, the models and the llama.cpp build in `~/llama.cpp` already belong to `pi`. A separate user would need its own copy of, or permissions on, all three for no gain on a single-purpose machine. `install.sh --user NAME` installs for another user |
| Source | `/opt/epitaph/src`, owned by `pi` |
| Python | `/opt/epitaph/venv` (owned by `pi`): the package installed editable from the source, with the `display` extra, so `config/` and `bench/` sit next to the code and a deploy takes effect at the next start. Rebuilt only when `pyproject.toml`, the extras or Python change |
| State | `/var/lib/epitaph` (lives, counter, status); models in `/var/lib/epitaph/models/<model>/<quant>.gguf` |
| Units | `epitaph-controller.service` (Type=notify, WatchdogSec=30, Delegate=yes, CPUAffinity=0, Restart=always, OOMPolicy=continue, runs `epitaph run`) and `epitaph-display.service` (`ExecCondition=epitaph display --screen-present`: headless, the unit is skipped, not failed), in `/etc/systemd/system`, with the screen hot-plug pieces (`epitaph-display-hotplug.{service,timer}`, the helper in `/usr/local/sbin`, the udev rule; see "Offline operation"). Installed **disabled**; `install.sh --enable` enables the controller, the display and the hot-plug timer at boot |
| CPU clock (ADR-025) | `/usr/local/sbin/epitaph-clock <MHz>` (600-1800) or `reset`: root-owned, writes `scaling_max_freq` on every policy, accepts nothing else. `/etc/sudoers.d/020_epitaph-clock` lets the service user run exactly that without a password (the rule only matches `reset` or a 3-4 digit number). The unit resets the clock before every start and after every stop; the body resets it at every death and at every start |
| Watchdogs | The controller pings systemd (WATCHDOG=1) at least every 5 s through `epitaph.controller.sd_notify`, and stops when its life loop stops moving (systemd then restarts it); it sends STOPPING=1 on SIGTERM; below it the hardware watchdog (`RuntimeWatchdogSec=1m`, OS default), which `install.sh` checks |
| Network block (ADR-005) | `/usr/local/sbin/epitaph-netblock add\|del <cgroup>\|status`: root-owned, loads nftables table `inet epitaph`, chain `output`: per creature cgroup (`socket cgroupv2 level 3`), every outbound packet not for 127.0.0.0/8 or ::1 is rejected (TCP: connection refused at once). It accepts only `system.slice/epitaph*.service/creature`. `/etc/sudoers.d/021_epitaph-netblock` lets the service user run exactly that. nft stores the cgroup's id, so the body loads the rule at every controller start (a restart makes a new cgroup) and keeps the creature cgroup across lives; each call drops the rules of cgroups that are gone. The controller refuses to start when the rule does not load. DNS goes straight to the router's servers (no local resolver), so it is refused too |
| RAM death calibration (C7) | `epitaph calibrate --user pi` (controller stopped; it takes the instance lock): per ladder step, load, 8 tokens, working set, then `memory.max` at half the anonymous memory; 5 good kills in a row at the death step. Qwen3 4B, `dio`, ctx 2048 (2026-10-01): Q4_K_M anon 2739 MiB, Q3_K_M 2331, Q2_K 1940; death levels 1369 / 1165 / 970 MiB; every kill 0.27-0.38 s, 7 of 7. Result in `/var/lib/epitaph/calibration/` and `bench/calibration/`; the body uses it when it is below the creature's anon. About 7 minutes |
| Thermal | `body/thermal.py`: temperature from `thermal_zone0`, firmware bits from `vcgencmd get_throttled` (no sysfs `get_throttled` on this kernel). A pause hook at `thermal_limit_c` 80 °C, resuming at 75 °C; the Pi runs 40-57 °C, so it never fires in normal use |
| Selftest | `epitaph selftest [--user pi]` relaunches itself as a transient `Delegate=yes` unit for the service user (`sudo systemd-run --uid … --pipe --wait --collect`) and checks the controllers, the leaves, limits set and cleared, `cgroup.kill`, the progress counters, the creature's network (the rule names its cgroup id; an outbound connect from inside is refused, 127.0.0.1 answers), the clock round trip and the llama-server binary; exit 0 or 1. `install.sh` runs it last |

## Offline operation (2026-10-01, from the laptop; Pi checks pending)

The installed piece runs with no network at all ([INSTALLATION.md](INSTALLATION.md) "Offline").

| Item | Value |
|---|---|
| Boot | No epitaph unit wants or waits for `network-online.target`. The controller is `After=time-sync.target` only (ordering, never a dependency): with `systemd-timesyncd` that target is reached once the saved clock is restored, synced or not. `install.sh` fails if `systemd-time-wait-sync.service` is enabled (it would hold that target until NTP answers: forever, offline). Before this round the controller wanted `network-online.target`, so an offline boot waited for `NetworkManager-wait-online` to give up before the first life |
| Clock without a network | No RTC: offline the kernel starts from a restored time, not the real one. Seen on 2026-09-30: a boot with no route out came up 88 min slow, not at the image date, so a saved clock is restored. Which keeper does it on this image (`fake-hwclock`: `/etc/fake-hwclock.data`, saved hourly and at shutdown; `systemd-timesyncd`: `/var/lib/systemd/timesync/clock`, whose mtime systemd restores at boot) is to be confirmed on the Pi; `install.sh` prints it (`note clock across offline reboots`). After a power cut the clock is behind by up to one save interval plus the time the Pi was off |
| What depends on the wall clock | Nothing a visitor sees, and nothing that keeps the piece safe. The life clock, hang detection, watchdog pings, the silence and the pacing use `time.monotonic` (`clock.RealClock`, `controller._loop_time`). Lives are numbered folders. Exhibition hours apply only while the kernel reports NTP sync (`exhibit.ntp_synced`, adjtimex), so with a restored clock the piece stays on. Wall time appears only as `ts` in events, `closed_ts` in death records and screenshot names; the display and replay place events by the life clock `t` and ignore steps in `ts` |
| Screen hot-plug | `epitaph-display.service` decides at boot (`ExecCondition`). After boot, `/etc/udev/rules.d/90-epitaph-display.rules` queues `epitaph-display-hotplug.service` on every DRM `change` uevent (vc4 KMS sends one per connector hot-plug), and `epitaph-display-hotplug.timer` runs it every minute as a backstop. `/usr/local/sbin/epitaph-display-hotplug` acts only when the connector states in sysfs changed: a screen appears, so it starts the display (only while the controller runs) or restarts it for a fresh mode; the screen goes, so it stops the display. A run with no change reads a few sysfs files and logs nothing (`LogLevelMax=notice`) |
| Why udev and a timer | udev alone is immediate but can lose an event (a `start` of a oneshot that is already running is merged into it) and relies on the sink raising hot-plug detect; a timer alone is up to a minute late. Both: a second or two normally, a minute at worst, and no Python unless a connector changed |
| Unplugged screen | The display either keeps running (the hot-plug helper stops it) or fails: `Restart=on-failure` restarts it once, its `ExecCondition` then finds no screen and skips the unit (inactive, not failed). No crash loop |
| Display on the console | `SDL_VIDEODRIVER=kmsdrm`, console boot (no X, no Wayland), as the service user with `SupplementaryGroups=video render input`; the bus on `127.0.0.1:7707` (loopback stays up with networking off) |
| Offline test | `tools/offline_pi.sh` (from the laptop, under the Pi lock) arms `epitaph-offline-rescue.timer` (enabled, `OnActiveSec=N min`, so it also fires N min after any boot; the service runs `nmcli networking on`, retried every 30 s, then disables its timer), verifies it, then runs `nmcli networking off` (and `systemctl reboot` with `--reboot`) from a transient unit 5 s later, waits for the Pi and reports. `--disarm` removes the rescue |

## Fault rows on the Pi (`tools/fault_pi.sh`, BUILD_PLAN 10.4)

Run under the lock against the installed service: `tools/pi_lock.sh run <agent> 60 --
tools/fault_pi.sh all` (or one row). Each row prints its evidence and `PASS`/`FAIL`; it never
stops the controller, and leaves it running whatever happens. First run, 2026-10-01, on the
phase 2 branch (pi4/default lives, Qwen3 4B):

| Row | Evidence | Result |
|---|---|---|
| netblock | rule on the creature cgroup (id 9601); from inside: 1.1.1.1:443 `ConnectionRefusedError`, 127.0.0.1 (own listener and llama-server :8081) connected; from the SSH session: all connected | PASS |
| two-controllers | `epitaph run` beside the service: "a controller is already running (pid 3008); use `epitaph ctl new-life` …", rc 1; service pid and life unchanged | PASS |
| crash | `kill -9` of the creature at t=61 s of life 8: `death.json` cause `crash` 3 s later; life 9 living 218 s after the kill (silence 90 s + load) | PASS |
| controller-kill | `systemctl kill -s KILL epitaph-controller` in life 9: restarted (pid 3008 → 4473, NRestarts 0 → 1); life 9 closed `interrupted`; life 10 next; one llama-server; clock 1800 MHz; the network rule loaded again on the new creature cgroup (id 15230) | PASS |
| hang | `kill -STOP` of the busy creature at t=64 s of life 11: cause `hang` after 308 s (the first-token limit: the stop came during prompt processing); the stopped process killed by `cgroup.kill`; life 12 born 522 s after the stop | PASS |

Budget: about 30 minutes for `all` (each of crash, controller-kill and hang ends a life and waits
for the next birth; hang may start a fresh life with `ctl new-life` and waits up to the
first-token limit).

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
