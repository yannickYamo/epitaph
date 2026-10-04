# Changelog

## Phase 0a
- Contracts in code: `types.py`, `config.py` (profiles with `extends`, fractional and end-anchored keyframes, overlays, fail-fast validation), `clock.py` (Real, Fake, Rehearsal clocks; Schedule with recall and CPU share held until reloads), `costmodel.py` (`epitaph estimate`: the thought-count rule), `state.py`, `events.py` (bus with per-subscriber bounded queues, control channel), `backend/base.py`, `body/base.py`, fakes, `sim.py` (reference loop), `cli.py`.
- Profiles retimed by the cost model: `compressed-2700` failed rules (a) and (b) with estimated costs; decline moved to 20:00, reload 2 to end-20:30, erosion from end-13:00. `skeleton-1200` last change moved to 16:00.
- Protocol version 1.

## Checkpoint A (2026-09-30)
- The owner chose **Qwen3 4B Instruct 2507**, the owner's original persona and chat mode, after reading
  rehearsed lives of eight models (docs/CHECKPOINT_A.md).
- The 4B runs on the Pi 4 on its own schedule with the memory carried across reloads: 29 thoughts
  in the rehearsed hour, reload silences 101 s and 116 s.
- Next: a voice closer to Latent Reflection's (introspective, questioning, poetic, facing its end
  without announcing it), then the schedule fitted to the 4B for every profile.

## The voice and the 30-minute life (2026-09-30)
- A thin prompt (50 words) and quiet readings: the full picture at birth, then only what was
  taken (ADR-023; prompt log round 4).
- Material readings: a reading quotes the opening words of each forgotten thought, and after a
  reload, five of its own words as the degraded weights continue them (ADR-026).
- The life is 30 minutes; thought-count minimums are set per profile (ADR-024, PROFILES.md).
- The CPU clock is a decay knob, linear in speed on the Pi 4 (spike S7, ADR-025).
- No temperature in the readings; silent word penalties against clichés.
- Blind panel of three judge models: the three new 30-minute lives scored 39.3, 29.7 and 29.0 of
  60 against 23.7 for the best one-hour life (prompt log round 5).


## Phase 1: the walking skeleton on the Pi (2026-10-01)
- The controller: the life loop with every death cause, hang detection, a watchdog that
  tracks the loop's progress, recovery after a power cut, the control channel; the simulator
  and the rehearsal run on it.
- Installed as systemd services on the Pi (`deploy/install.sh`, `tools/pi_deploy.sh`), with a
  narrow helper for the CPU clock and `epitaph selftest`.
- The local display starts only when a screen is connected; the remote view works over SSH.
- Gate G1 passed on the Pi: a smoke life, two 20-minute lives, the remote view live, a headless
  reboot.

## Phase 2 closed, phase 3 and offline (2026-10-01)
- Gate G2 on the Pi: three consecutive 30-minute lives pass at the full level; the fault
  matrix (network block, crash, hang, controller killed, two controllers) passes.
- Fixes from real lives: each keyframe's clock and CPU share applied on time; the last words
  on screen within the display limit after a death; a letter ceiling at 2-bit.
- Offline: the Pi boots and lives with no network; a screen plugged in later starts the
  display; each life's last words are kept on disk for future posts (docs/AFTERLIFE.md).
- Exhibition hours; the arm64 install test; the soak tools; user docs (README, CONFIG,
  INSTALLATION, CONTRIBUTING).
- The original persona keeps its knowledge of its end to the last erosion step (ADR-011);
  no 25-hour soak (ADR-029).

## v1.0 (2026-10-01)
The piece as the owner approved it: Qwen3 4B lives 30 minutes on the Raspberry Pi 4; the model
never changes during a life; only the machine shrinks around it, for real and from the outside
in (processes stopped, its radio and light switched off, its screen dimmed, its CPU and clock
cut, its memory cut, then its RAM taken and the kernel's kill). It is told what it senses, said
to "you" and in proportions of what it had when it woke, and nothing tells it how to feel. Its
words stream at about 33 words a minute at birth and slow smoothly with the machine, never
stopping until the death. It runs offline, keeps each life's last words for a future feed, and
starts the next life within a minute of the last.

## Small chips (2026-10-03)
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
  token 39 times in 40. It compiles for an ESP32 Dev Module (577 KB flash, 305 KB heap free)
  and lives a whole simulated life on a 300 KB heap (`make badge`, in `make check` and CI). Not
  yet run on a board.
- Tufty: lives stopped at about five minutes. The likely cause, not yet confirmed on the badge:
  the cache was reallocated at each context reset and fragmented the heap. It is now allocated
  once, a passing memory error no longer ends a life, and errors go to `/epitaph_errors.log`.

## Readings that tell the truth, and ports (2026-10-04)
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
- Ports: the life no longer knows a board. In C, three required hooks and four optional ones; in
  MicroPython, `life.py` and one `Board` class, with `terminal.py` as the smallest port. On a
  Linux board, `epitaph probe` reads what the machine has to lose and prints its overlay.
  [PORTING.md](PORTING.md) is the guide.
- `make badge` now runs whole lives on a simulated ESP32, a board with only a serial port and
  the Tufty badge, and fails unless each ends as it must. The sketch compiles for an ESP32 and
  a Raspberry Pi Pico. None of this has run on a real small board yet.
