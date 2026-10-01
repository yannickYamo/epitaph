# Installation

## Install on a Pi

From a freshly flashed card to a creature living on the machine. The reference is a Raspberry Pi
4 Model B with 4 GB on Raspberry Pi OS Lite 64-bit (Debian 13 trixie), the official 5.1 V / 3 A
USB-C supply and a 64 GB card; [PI_FACTS.md](PI_FACTS.md) records that machine. The install is
one idempotent script, `deploy/install.sh`: it can be run again at any time and reports
`changed: 0` when nothing needed doing. It is checked on the Pi and, on every change to it, in a
clean arm64 Debian container (`make install-test-arm64`, below).

Plan on about 2 hours, most of it the llama.cpp build and the model download. Commands run on
the Pi as its first user (`pi` below; `install.sh --user NAME` installs for another one).

**Power first.** Use the official supply. On a weaker one the Pi browns out and reboots under a
four-core load such as the llama.cpp build (`vcgencmd get_throttled` shows `0x50000`).

### 1. Flash and boot

Write Raspberry Pi OS Lite (64-bit) with Raspberry Pi Imager. In its settings, create the user,
enable SSH with your public key and enter the Wi-Fi network. Boot, log in, then:

```sh
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y git build-essential cmake
```

### 2. The memory cgroup and console boot

The RAM death needs the cgroup v2 `memory` controller, which the Pi firmware disables at boot
(it adds `cgroup_disable=memory` to the kernel command line). The standard override is two flags
appended to the single line of `cmdline.txt`. Console boot leaves about 350 MB more for the model.

```sh
sudo cp /boot/firmware/cmdline.txt /boot/firmware/cmdline.txt.bak
grep -qw cgroup_enable=memory /boot/firmware/cmdline.txt \
  || sudo sed -i '1 s/$/ cgroup_enable=memory cgroup_memory=1/' /boot/firmware/cmdline.txt
sudo systemctl set-default multi-user.target
sudo reboot
```

After the reboot, `cat /sys/fs/cgroup/cgroup.controllers` must list `memory` (with `cpu` and
`io`). If it does not, check that the flags sit on the file's one line (`cat
/proc/cmdline` shows what the kernel received) before going on: the controller refuses to start
on a Pi without the controllers it takes the creature's resources through.

`tools/pi_bootstrap.sh` applies these settings and the rest of the reference machine's setup
(hostname, swap, journal, SSH policy; BUILD_PLAN 8.6) idempotently from a laptop. It encodes
that installation's site choices, so read it before pointing it at another Pi.

### 3. The source

The services run the code from `/opt/epitaph/src`; `install.sh` refuses to run from anywhere
else.

```sh
sudo install -d -o pi -g pi -m 0755 /opt/epitaph
git clone https://github.com/yannickYamo/epitaph /opt/epitaph/src
```

### 4. llama.cpp

`tools/build_llamacpp.sh` builds the release pinned in `config/models.toml` into `~/llama.cpp`
(about an hour at four cores). Run it as a transient unit so that a dropped SSH session does not
stop it:

```sh
sudo systemd-run --unit=llama-build --uid=pi --gid=pi --setenv=HOME=/home/pi \
  --working-directory=/home/pi bash /opt/epitaph/src/tools/build_llamacpp.sh
tail -f ~/llama.cpp/build.log                    # until "built llama.cpp ..."
~/llama.cpp/build/bin/llama-server --version
```

### 5. Install

```sh
sudo /opt/epitaph/src/deploy/install.sh
```

It installs, only where something differs:

| What | Where |
|---|---|
| Missing packages (`python3-venv`, `nftables`, `sudo`) | apt |
| The Python environment, epitaph installed from the source (with the `display` extra) | `/opt/epitaph/venv`, owned by `pi` |
| State and models | `/var/lib/epitaph`, `/var/lib/epitaph/models`, owned by `pi` |
| The CPU clock helper and the creature's network block (root-owned, accept nothing but their own arguments; ADR-025, ADR-005) | `/usr/local/sbin/epitaph-clock`, `/usr/local/sbin/epitaph-netblock` |
| One sudoers rule per helper, checked with `visudo -cf` before it is installed | `/etc/sudoers.d/020_epitaph-clock`, `/etc/sudoers.d/021_epitaph-netblock` |
| The controller and display units, disabled | `/etc/systemd/system/epitaph-{controller,display}.service` |

Then it checks the cgroup controllers, the hardware watchdog (Raspberry Pi OS enables it, 1
minute), the llama-server binary and the units (`systemd-analyze verify`), and runs `epitaph
selftest` last. It ends with `changed: N` and `failed: N`, and exits 1 on any failure.

### 6. Models

The model files are not in the repository; each one is pinned by sha256 in
`config/models.lock.toml` and checked after download. The Pi 4 ladder of the chosen model (Qwen3
4B Instruct 2507 at Q4_K_M, Q3_K_M and Q2_K) is about 6.5 GB:

```sh
cd /opt/epitaph/src
EPITAPH_MODELS_DIR=/var/lib/epitaph/models /opt/epitaph/venv/bin/python \
  tools/download_models.py fetch --models qwen3-4b-instruct-2507 --quants ladder
```

From a laptop with the repository instead: `tools/download_models.py fetch` with the same
arguments, then `push --host <ssh alias of the Pi>` (rsync, then sha256 on the Pi).

### 7. Calibrate the RAM death (recommended)

```sh
/opt/epitaph/venv/bin/epitaph calibrate --user pi
```

About 7 minutes: it loads each ladder step, measures its working set and finds the memory limit
that kills it within seconds, five times in a row. The result goes to
`/var/lib/epitaph/calibration/`; without it the body uses the levels measured on the reference
Pi (`bench/calibration/`), which suit the same board, model and llama.cpp release.

### 8. Start

```sh
sudo /opt/epitaph/src/deploy/install.sh --enable    # enable both units at boot
sudo systemctl start epitaph-controller
```

The display unit starts only when a screen is connected; headless, systemd skips it cleanly. To
watch from a laptop: `epitaph display --connect <ssh alias of the Pi>`.

### 9. Verify

```sh
/opt/epitaph/venv/bin/epitaph selftest --user pi    # exit 0: cgroups, limits, kill, network block, clock, llama-server
sudo /opt/epitaph/src/deploy/install.sh --check     # changed: 0, failed: 0
systemctl status epitaph-controller                 # active (running)
/opt/epitaph/venv/bin/epitaph ctl status            # the life, its age and state
journalctl -u epitaph-controller -f                 # the controller's log
```

The first words appear about 4 minutes after power-on (`first_word_after_boot_s`, 240 s): the
model load, then the first thought.

### Updating

```sh
cd /opt/epitaph/src && git pull
sudo /opt/epitaph/src/deploy/install.sh --enable && sudo systemctl restart epitaph-controller
```

From a development laptop, `tools/pi_deploy.sh` does the same with the working tree: it rsyncs it
to `/opt/epitaph/src` and runs `install.sh` there, passing its options through. It runs under the
Pi lock, which serialises every agent's use of the Pi:

```sh
PI_HOST=<ssh alias> tools/pi_lock.sh run <name> 15 -- tools/pi_deploy.sh --enable
```

### The arm64 install test

`make install-test-arm64` (`tools/test_install_arm64.sh`) runs the install in a clean
`debian:trixie` arm64 container on an x86 laptop, emulated by qemu-user-static under podman: a
first run, a second that must report `changed: 0`, a `--check` that must find no drift, then
checks of the venv, the units, the helpers and the sudoers rules (`visudo -cf`, and what `pi`
may and may not run). `install.sh --container` skips only what needs a booted systemd or the Pi
itself: `daemon-reload`, the cgroup and watchdog checks and the selftest (`systemd-analyze
verify` reads the unit files offline and runs there too). llama-server is a stub. It takes
about 10-12 minutes on a 12-thread laptop, most of it emulated apt and pip, so it is not part
of `make check` or CI.
