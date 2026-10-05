# Porting epitaph to another board

**The piece is one rule: the model never changes, and the machine around it is taken away for
real. A port describes what its board can take. Everything else is shared, and whatever a board
cannot do is skipped and never reported.**

There are three ways in, by the size of the board.

| Your board | Edition | What you write |
|---|---|---|
| Runs Linux, 4 GB of RAM or more (a Pi 5, another single-board computer, a small PC) | The installation: Qwen3 4B under a prompt | One hardware overlay file |
| A microcontroller with a C compiler, 600 KB of flash and 100 KB of free RAM or more | The C port: a 260K-parameter model, int8 | Four required hooks (clock, output, random, heap), up to four optional ones |
| Runs MicroPython with `ulab` | The MicroPython port: the same model, float32 | One small class |

The schedule, the readings, the model and the death are the same code on every board. You do
not edit them.

## The rule every port keeps

A reading tells the model only what the board did a moment before. So each loss is a function
that performs it and answers whether it happened:

- It returns true only when the board really did it (the clock really changed, the LED is
  really off).
- A board with no such hardware leaves the function out, or returns false. That step of the
  schedule is skipped and no reading is written for it.
- The memory window and the RAM are taken by the shared code on every board, so every port has
  a life that forgets and a death.

## A Linux board

The installation runs on a Raspberry Pi 4. On another Linux board, ask the machine what it has
to lose before installing anything. The command only reads.

```sh
git clone https://github.com/yannickYamo/epitaph && cd epitaph && make venv
.venv/bin/epitaph probe --name myboard
```

```text
a machine without a device tree (a PC?), 12 cores, 31.0 GB

What a life can lose on this machine:
  yes  services               cron, bluetooth, avahi-daemon, ModemManager, cups
  yes  processes around it    414 run now
  yes  radio                  Wi-Fi enabled
  no   light                  no ACT or PWR LED (this board has phy0-led: name them as LED_NAMES ...)
  yes  screen                 connected
  yes  CPU share              cgroup v2 cpu controller
  yes  CPU clock              400 to 5200 MHz
  yes  RAM (the death)        cgroup v2 memory controller

# config/hardware/myboard.toml: start from pi4-4gb.toml and set these.
[body]
clock_helper = "/usr/local/sbin/epitaph-clock"

[world]
helper = "/usr/local/sbin/epitaph-world"
services = ["cron", "bluetooth", "avahi-daemon", "ModemManager", "cups"]
```

That is a laptop. Its lights have other names than the Pi's, so its light is never taken and
never mentioned; everything else is there.

Then:

1. Copy `config/hardware/pi4-4gb.toml` to `config/hardware/myboard.toml` and paste the lines
   the probe printed over the same keys. Keep `class = "pi4"` to live the Pi 4's schedules.
2. Follow [INSTALLATION.md](INSTALLATION.md), "Install on a Pi". Set `hardware = "myboard"` in
   `config/default.toml`, or pass `--hardware myboard`.
3. Run `epitaph calibrate` on the board. It measures the memory limit that kills the model
   within seconds on this machine.
4. Run `epitaph estimate --profile pi4/default --hardware myboard --fit-pace`. The typing pace
   is fitted to how fast the board generates. The `[costs]` table in the overlay holds those
   speeds; replace the Pi 4's numbers with yours once you have measured a life.

A line that says `no` needs nothing from you. Only the last one is required: without the cgroup
v2 memory controller there is no death, and the probe exits 1.

## A microcontroller, in C

The C port is three files in [`badge/esp32/epitaph_esp32`](../badge/esp32/epitaph_esp32):
`epitaph_tiny.c` (the model and the life), `epitaph_tiny.h` (the hooks) and `model_data.h` (the
weights). Nothing in them knows a chip. A port fills in one struct:

| Hook | Required | What it does |
|---|---|---|
| `millis`, `sleep_ms` | yes | A millisecond clock and a sleep |
| `write` | yes | Where its words go: a serial port, a display |
| `random32` | yes | A random number |
| `alloc`, `release` | yes | The heap. `alloc` returns NULL when it is full: the life takes it at the end, and that NULL is the death |
| `set_light` | no | An LED on or off. Returns 1 when done |
| `set_clock_mhz` | no | The CPU clock. Returns 1 when it really changed. Set `full_mhz` and `clocks` in `ep_config` to your chip's steps |
| `set_screen` | no | A backlight, in percent. Returns 1 when done |
| `event` | no | Told each loss, each reading and the death, for a log |

Then call `ep_init(&platform)` once and `ep_live(&platform, &config, n)` for ever.

RAM: the memory window takes 1,280 bytes per position, `EP_SEQ` positions (128 by default, so
164 KB), plus 5 KB per thought. On a smaller chip lower `EP_SEQ` in `epitaph_tiny.h`: 64
positions take 82 KB. Its memory is then cut twice in a life, not four times: a window too
small to shrink again is left alone. Flash: 600 KB with the Arduino core.

Two ports ship as examples:

- [`epitaph_esp32.ino`](../badge/esp32/epitaph_esp32/epitaph_esp32.ino), an Arduino sketch. On
  an ESP32 it takes the LED and the clock (compiled, not yet run on a board). On any other
  Arduino board it takes the LED and leaves the clock alone until you write `set_clock` for the
  chip.
- [`host/test_host.c`](../badge/esp32/host/test_host.c), a laptop with a simulated heap and
  clock. `make -C badge/esp32 life` runs a whole life on it and checks it; `./host/test_host
  bare` runs one on a board with nothing but a serial port.

## A MicroPython board

The MicroPython port is the folder [`badge/tufty/epitaph`](../badge/tufty/epitaph): `life.py`
(the life), `tinyllama.py` (the model) and `assets/` (1 MB). It needs `ulab`, which Pimoroni's
firmware builds include. Nothing in `life.py` knows a board.

Copy the folder to the board and run `terminal.py`. With no change it lives in the serial
terminal, loses its memory window, and dies when its heap is taken. Try it on a laptop first
(a laptop's heap cannot be taken, so there the life ends at its deadline):

```sh
python badge/tufty/epitaph/terminal.py 60 1     # a 60-second life, once
```

To give it more to lose, fill in the `Terminal` class in `terminal.py`, a `life.Board`:

| Method | What it does |
|---|---|
| `light(on)` | An LED. Return True when done |
| `screen(level)` | A backlight, 1.0 is full. Return True when done |
| `clock(mhz)` | The CPU clock, one of `CLOCKS` (full speed first). Return True when it really changed |
| `free()`, `alloc(n)` | The heap. The defaults use `gc`; leave them |

To draw on a screen instead of the serial port, give `Life` an `out` with three methods:
`begin()` opens a paragraph, `face(text)` sets the face on its first line, `write(text)` adds
words. The Tufty badge's app, [`__init__.py`](../badge/tufty/epitaph/__init__.py), is that: a
`Tufty` board of 20 lines and a `Page` that wraps words on its screen.

## Check a port before the board

`make badge` runs both small-chip ports through whole simulated lives and fails unless each
life went as it must: every loss performed and read once, the last reading read and answered,
a death by memory, the board left as it was found. Run it after any change to the shared code.

What a simulation cannot tell you is the board's speed and how its heap fragments. After the
first real life, read the log (`event` in C, `Life.trace` in MicroPython): the last line must be
a death by memory, not a deadline.
