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

Exhibition hours are part of phase 3 (BUILD_PLAN 5.10): the settings are defined, and the
controller honours them once that work lands. Until then, switching the screen off outside the
hours gives the `unseen` behaviour.

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

The scripts are idempotent: run again, they change nothing. In order:

1. Flash Raspberry Pi OS 64-bit, enable SSH with a key, and join Wi-Fi.
2. `tools/pi_bootstrap.sh --apply <host>` from a laptop: memory cgroups, console boot, the
   hardware watchdog, a persistent journal, key-only SSH over Wi-Fi.
3. `tools/build_llamacpp.sh --pi`: llama.cpp at the tag pinned in `config/models.toml`, built on
   the Pi (about an hour).
4. `tools/download_models.py fetch` then `push`: the model files, checked against the sha256
   pinned in `config/models.lock.toml`.
5. `tools/pi_deploy.sh --enable` (through `tools/pi_lock.sh`): copies the source to
   `/opt/epitaph/src` and runs `deploy/install.sh`, which installs the services, the clock and
   network helpers and the state directory, then runs `epitaph selftest`.
6. `epitaph calibrate` once, the controller stopped: the RAM death level for each precision.

[PI_FACTS.md](PI_FACTS.md) describes the installed system in detail.
