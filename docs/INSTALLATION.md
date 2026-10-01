# Installation

How to set *epitaph* up in a room: what it needs, where it goes, and what to put on the wall.
The piece is designed to run unattended all day, every day: a life is 30 minutes, a new one is
born 90 seconds after each death, and the machine recovers on its own from a crash or a power
cut.

## What you need

| Item | Why |
|---|---|
| Raspberry Pi 4 Model B, 4 GB | The tested target (ADR-001). A Pi 5 runs the same code but is only simulated so far |
| **The official Raspberry Pi 5.1 V / 3 A USB-C power supply** | See [Power](#power): an ordinary phone charger browns the Pi out under load |
| A microSD card, 32 GB or more | Holds the system, the model (three precision steps, about 2 GB each) and every life's transcript |
| A screen, optional | Any HDMI screen; the display is tested at four resolutions from 800×480 to 1080×1920, in landscape or portrait. Without one, the piece runs headless and can be watched from a laptop |
| A micro-HDMI to HDMI cable | The Pi 4 has micro-HDMI ports |
| A case, optional | Passive is enough ([Cooling](#cooling)) |
| An Ethernet cable, optional | A maintenance link to a laptop ([Network](#network)) |

## Placement

- **The screen is the piece.** Put it at eye level where people can stand in front of it for a
  few minutes. The text is drawn no smaller than 36 px with a contrast of 16.9:1, so it reads from
  a few metres; avoid glare from windows and spotlights.
- **The Pi can be seen.** The work is about a model dying on hardware you could hold in your
  hand. Showing the board next to the screen helps visitors connect the two, but keep it out of
  reach: a visitor pulling the card or the power ends more than one life.
- **Give it air.** Not in a closed box, not against a heater or in direct sun.
- **Leave the cables tidy and secured.** The power cable especially (see below).

## Power

Use the official 5.1 V / 3 A supply, plugged straight into the wall, not into a USB hub or a
screen's USB port.

During development, an under-rated supply made the Pi 4 brown out and reboot under three-core
load; the firmware recorded under-voltage (`throttled=0x50000`). With the official supply, a
30-minute life at full load shows no under-voltage bit (`throttled=0x0`). The 25-hour soak
before a show checks this continuously; to check by hand:

```sh
ssh pi vcgencmd get_throttled        # throttled=0x0 is healthy; bit 0 or 16 set is under-voltage
```

The controller is built to survive a power cut: at the next boot it closes the life in progress
as `interrupted` and the next life is born; the target is the first word on screen within four
minutes of power returning (`first_word_after_boot_s`, measured at the last gate). Still,
the SD card is the weak point of any Pi under repeated power loss: switch the piece off at the
end of a show with `sudo poweroff` rather than at the wall when you can, and keep an image of the
card (`tools/sd_backup.sh`).

## Cooling

No fan is needed. Over 30-minute lives at full load the Pi 4 stays between 40 and 57 °C with no
throttling (spikes S1c and S7), far below the firmware's 80 °C limit. As a safety net the
controller pauses between thoughts at 80 °C and resumes at 75 °C (`[body] thermal_limit_c`,
`thermal_resume_c`); the soak report counts any time spent throttled or paused. In a room much
warmer than 30 °C, or in a closed case, a passive heatsink case is a cheap margin.

## Network

- **Wi-Fi** is the Pi's only route to the internet. It needs it for one thing: the time (NTP),
  because the Pi has no real-time clock and exhibition hours use the local time.
- **The model itself has no network.** An nftables rule refuses every outbound connection from
  its process group (ADR-005); nothing it writes can leave the machine.
- **Nothing listens to the outside.** The event stream and the control channel listen on
  `127.0.0.1` only. A laptop watches through SSH (`epitaph display --connect <host>`), with keys
  only over Wi-Fi.
- **A cable for maintenance.** An Ethernet cable from a laptop sharing its connection (a
  `10.42.0.x` address) reaches the Pi when Wi-Fi does not; it never becomes the default route.

Without any network, lives go on; only the clock drifts, and exhibition hours are switched off
until the time is synced again.

## Exhibition hours

By default the piece runs around the clock. `[exhibit]` in `config/default.toml`
([CONFIG.md](CONFIG.md)) defines opening hours, for example `hours = "10:00-18:00"`, and what
happens outside them:

- `outside = "unseen"` (default): lives go on with the screen dark, so the piece is never
  paused, only unwatched.
- `outside = "pause"`: the life in progress finishes, then no new life is born until opening.

Hours apply only while the Pi's clock is synchronised over the network (NTP); with no synced
time the piece stays on and logs a warning once. A request for a new life
(`epitaph ctl new-life`) during closed hours starts one anyway.

## Wall label

A suggested label. The lifespan and the model are the installation's defaults; change them if
the configuration changes.

> ***epitaph***
>
> Raspberry Pi 4, screen, Qwen3 4B Instruct (a small language model), software. 2026.
>
> A small language model lives on this computer for thirty minutes. As it lives, the machine
> takes its resources away: the memory it can hold, the precision of its weights, the share and
> the speed of its processors, and, at the end, the memory it needs to exist. Each time it is
> told exactly what it has lost, down to the words it forgot, and its thoughts appear here one
> letter at a time. Nothing is staged: every loss really happens to it. When it dies, the screen
> goes dark. Ninety seconds later, a new one is born, and remembers nothing.
>
> After *Latent Reflection*, which runs a language model on a Raspberry Pi until its memory
> runs out.

## Credits

For the label, a catalogue or a website:

- *epitaph* by Yannick. Source code: MIT license.
- Inspired by **Latent Reflection**: a Raspberry Pi 4 running Llama 3.2 3B on a 96-character
  16-segment display, generating until its memory runs out.
- Model: Qwen3 4B Instruct 2507 by the Qwen team, Apache 2.0, downloaded at install time and not
  redistributed. Other models it can run keep their own licenses
  ([config/models.toml](../config/models.toml)).
- Inference: [llama.cpp](https://github.com/ggml-org/llama.cpp) (MIT).
- Typeface: IBM Plex Mono (SIL Open Font License).

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
