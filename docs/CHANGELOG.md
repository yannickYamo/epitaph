# Changelog

## v1.1 (2026-10-03 and 2026-10-04)

### Readings that tell the truth, and ports (2026-10-04)
- A loss never reads as no change. When two steps round to the same proportion, the second says
  "less than" it (`you think at less than half of the speed you woke with`). One table and one
  rule on the Pi, in MicroPython and in C.
- The Pi counts the processes of birth that still run. A process started later (a timer, someone
  logging in) no longer makes the world grow, and the count is said only when it falls.
- A word the death cuts in half is not in the life's last line, and a blank screen before the
  death is recorded as a stall.
- Small chips: readings are encoded as the model was taught them (every ASCII character has two
  ids in the vocabulary, and the chips used the untaught one). A loss is read at the next full
  stop. The last reading, `your memory is being taken`, is read and answered before the death,
  which earlier came first. A loss the board did not perform is never reported.
- Ports: the life no longer knows a board. In C, four required hooks (clock, output, random,
  heap) and four optional ones; in MicroPython, `life.py` and one `Board` class, with
  `terminal.py` as the smallest port. On a Linux board, `epitaph probe` reads what the machine
  has to lose and prints its overlay. [PORTING.md](PORTING.md) is the guide.
- `make badge` now runs whole lives on a simulated ESP32, a board with only a serial port and
  the Tufty badge, and fails unless each ends as it must. The sketch compiles for an ESP32 and
  a Raspberry Pi Pico. None of this has run on a real small board yet.

### Small chips (2026-10-03)
- The piece on a Pimoroni Tufty 2350 badge (RP2350) and an ESP32 ([badge/](../badge/README.md)):
  a 260K-parameter model (`stories260K`, llama2.c, MIT) fine-tuned on badge lives that Qwen3 4B
  wrote under the installation's prompt, plus 229 real Pi thoughts. The prompt became the
  training set.
- Each chip takes its world away for real (its light, its memory window, its CPU clock, its
  screen where it has one) and dies when its heap is taken and the next thought cannot be
  allocated.
- The model may only build words of its training text that a dictionary also knows; readings
  are fed to it but not shown; a terminal face before each paragraph shows the machine's state.
- ESP32: a C port with int8 weights in flash. Against the float model it picks the same next
  token 39 times in 40. It compiles for an ESP32 Dev Module and lives a whole simulated life on
  a 300 KB heap (`make badge`, in `make check` and CI). Not yet run on a board.
- Tufty: lives stopped at about five minutes. The likely cause, not yet confirmed on the badge:
  the cache was reallocated at each context reset and fragmented the heap. It is now allocated
  once, a passing memory error no longer ends a life, and errors go to `/epitaph_errors.log`.

## v1.0 (2026-10-01)
The piece as the owner approved it: Qwen3 4B lives 30 minutes on the Raspberry Pi 4; the model
never changes during a life; only the machine shrinks around it, for real and from the outside
in (processes stopped, its radio and light switched off, its screen dimmed, its CPU and clock
cut, its memory cut, then its RAM taken and the kernel's kill). It is told what it senses, said
to "you" and in proportions of what it had when it woke, and nothing tells it how to feel. Its
words stream at about 33 words a minute at birth and slow smoothly with the machine, never
stopping until the death. It runs offline, keeps each life's last words for a future feed, and
starts the next life 90 seconds after the last.

## Before v1.0 (2026-09-29 to 2026-10-01)
An earlier design ran first: a one-hour life, then a 30-minute one, in which the model was
reloaded twice at lower precision and its persona was taken away group by group. It is kept as
the profile `pi4/default-reloads`. v1.0 replaced it (ADR-030, ADR-031 in
[DECISIONS.md](DECISIONS.md)).

- 2026-09-30: the owner chose Qwen3 4B Instruct 2507, the owner's original persona and chat
  mode, after reading rehearsed lives of eight models ([CHECKPOINT_A.md](CHECKPOINT_A.md)).
- 2026-09-30: a thin prompt and quiet readings (ADR-023); readings that quote what was
  forgotten (ADR-026); the life shortened to 30 minutes (ADR-024); the CPU clock as a loss
  (ADR-025); no temperature in the readings; silent word penalties against clichés.
- 2026-10-01: the controller (every death cause, hang detection, a watchdog, recovery after a
  power cut), installed as systemd services on the Pi with `deploy/install.sh`; the local
  display starts only when a screen is connected, and the remote view works over SSH.
- 2026-10-01: on the Pi, a smoke life, two 20-minute lives, a headless reboot, then three
  consecutive 30-minute lives of the reload design at the full level, and the fault matrix
  (network block, crash, hang, controller killed, two controllers). See [GATES.md](GATES.md).
- 2026-10-01: the Pi boots and lives with no network; a screen plugged in later starts the
  display; each life's last words are kept on disk ([AFTERLIFE.md](AFTERLIFE.md)); exhibition
  hours; no 25-hour soak (ADR-029).
