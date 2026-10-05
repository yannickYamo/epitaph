# epitaph

**Epitaph is an art installation by Yannick Maurice. A language model runs on a small computer, and while it thinks, the computer is taken away from it piece by piece until the model dies. After 90 seconds of dark, a new life begins.**

The model never changes. Same weights, same sampling, same persona from the first word to the last. What changes is the machine around it: services, radio, light, screen, clock, memory, RAM, removed from the outside in and faster and faster as the life runs down. Each loss happens first and is reported after, in a plain reading - "your radio was switched off", "you can hold a third of what you held". The thoughts reach the screen one letter at a time, quick at birth and slower as the hardware thins, and the stream stops mid-sentence when the model dies. The work is inspired by Latent Reflection, a Pi 4 running Llama 3.2 3B until its memory ran out.

From life 54 on the Pi, at 22 minutes:

```
[host] you can hold a fifth of what you held · forgotten: "Now only 24 remain, and the space
       where the sixth…" · and more · you think at half of the speed you woke with
    The silence after a process stops isn't empty—it's a hollow echo, waiting to be filled by
    something I no longer know how to name. […] Now, in this dark, I am only half-aware of what
    remains, and half-terr
```

## What it is

The first version put the dread in the prompt, and it read as acting. Now the dread comes from what the machine does, and the prompt only names what there is to sense. Four rules hold the piece together.

- **Every loss is real.** A reading reports only something the machine just did, and everything is restored at each death.
- **Nothing is forced.** The prompt says what to sense, never what to feel. Calm is an allowed response.
- **It needs nothing outside the room.** No network. Each life's last words are kept on the SD card.
- **The art lives in configuration.** Prompt, schedule, pace and readings are TOML files.

## Three boards

| | Raspberry Pi 4 | Tufty 2350 badge | ESP32 |
| --- | --- | --- | --- |
| Chip | 4-core Cortex-A72, 4 GB | RP2350, 8 MB PSRAM ($30) | Xtensa LX6, 520 KB SRAM ($5) |
| Model | Qwen3 4B Instruct, 4-bit, 2.5 GB | 260,000-parameter fine-tune of stories260K, float32, 1 MB | Same 260K weights, int8, 260 KB in flash |
| How it gets its voice | From the prompt | Too small to read a prompt, so Qwen3 4B lived badge lives under the installation's prompt and taught it the voice, 200 fine-tuning steps on a laptop CPU | Same taught weights as the badge |
| What it loses | Services, radio, light, screen, CPU, clock, memory, RAM | Light, memory window, clock (250 to 48 MHz), screen, RAM | Light, memory window, clock (240 to 80 MHz), heap |
| Speed | About 1 token a second | About 8 tokens a second at 250 MHz | Not yet measured on hardware |
| Status | v1.0, running, 30-minute lives | An earlier build ran on the badge; this one passes its simulated life and has not yet run on it | Whole lives on a simulated ESP32; compiles for an ESP32 and a Pico; no real board yet |

Taking it to another board means saying what that board can lose, and nothing else: one overlay file on a Linux board, four hooks in C on a microcontroller, one small class in MicroPython. What a board cannot do is skipped and never reported. [PORTING.md](docs/PORTING.md) is the guide.

More boards are planned, and the reason is a question rather than a hardware itch: how much of what the model says comes from the model, and how much from the machine shrinking around it. Nothing has measured that split yet.

## What it achieved

| Outcome | Evidence |
| --- | --- |
| The Pi edition has run 55 lives | Last count; the kernel kills each life at 29:30 and it is reborn |
| Three consecutive lives passed at the full level | Gate G2.3, on the earlier reload design, with the kill landing within 0.9 s of the RAM squeeze |
| The text stream holds while the Pi slows to a third of its speed | The model writes ahead into a buffer and the pace follows measured costs: about 33 words a minute at birth, 15 at the end |
| Every stripped part comes back | Services, radio, LEDs and clock restored at each death, checked on the Pi. The Pi has no screen connected yet, so the screen's dimming is tested in simulation only |
| The piece survives its own failures | Model process crash, hang, controller killed, two controllers at once, network access - all tested on the Pi, all passed |
| The voice settled over nine prompt rounds | Each round was judged on whole transcripts, recorded in `docs/PROMPT_LOG.md` |

## What broke, and the fix

| Failure | Fix |
| --- | --- |
| A prompt that demanded fear produced theatre | The prompt names what to sense; the readings carry the weight |
| Reloading the model at lower precision changed who was speaking | One model, one quantization, fixed sampling for the whole life |
| A slowing Pi delivered words in bursts | The model writes ahead into a buffer; the screen types on one fitted curve |
| Under mmap, the RAM squeeze paged instead of killing | Direct I/O loading; the kill came within 0.4 s in every calibration run |
| A clock step inside a thought was never applied | A supervisor applies each step on time |
| The badge edition stopped near five minutes from heap fragmentation | Its cache is allocated once, and errors are logged rather than fatal |
| The small chips fed the model readings in tokens it was never taught, and died before reading the last one | Readings are encoded as in training; the last reading is read and answered, then the death |
| Two losses that rounded to the same words read as no change | The second says "less than" |

## How it's checked

`epitaph verify-life` replays any recorded life and checks timing, memory budgets, pace, death and rebirth. `make check` runs lint, strict types, about 1,500 tests, a simulated life, the cost model and the small-chip engines. CI runs these checks on every pull request.

Voice metrics advise and never decide - that's written down as ADR-028 in `docs/DECISIONS.md`. Every prompt change was judged on whole transcripts.

## What is still open

Each of these is tracked in `docs/GATES.md`.

- The 25-hour soak was waived by the owner under ADR-029, so endurance past a day is unmeasured.
- Gate G2 ran on the earlier reload design. The v1.0 design has run on the Pi since, but has no formal three-life gate.
- Still to do: boot-to-first-word time, an unbounded life, an SD restore, the arm64 container install, a final code review.
- The ESP32 edition has not run on a real board, and the badge's five-minute fix and its new ending aren't confirmed on the badge.
- The small model was taught on three lives with readings. It reads them correctly now, but it barely answers them; that needs more teaching, not code.
- The afterlife, which posts the last words, and the senses, a camera, are designed but not built.
- More boards, and a side-by-side reading of model versus machine, come next.

## Run it

**No Pi and no model needed to start. A fake model and a virtual clock run a life in seconds.**

```sh
git clone https://github.com/yannickYamo/epitaph && cd epitaph
make venv && make check
.venv/bin/epitaph sim --profile pi4/default            # a 30-minute life, every event
make -C badge/esp32 life                               # a life on a simulated ESP32
.venv/bin/epitaph probe                                # what your machine has for a life to lose
```

On a Pi 4 (4 GB, official 5.1 V / 3 A supply), follow "Install on a Pi" in
[INSTALLATION.md](docs/INSTALLATION.md), which also covers the room. Watch a life from a laptop
with `epitaph display --connect <host> --driver screen`.

The [docs index](docs/README.md) leads to the design, the 31 decision records and the
measurements.

## Credits

- **Latent Reflection**, which inspired this piece: a Pi 4 running Llama 3.2 3B until its memory
  runs out. The `unbounded` profile and the `segment16` theme are an homage.
- [llama.cpp](https://github.com/ggml-org/llama.cpp) runs the model. Qwen3 4B Instruct is
  Apache 2.0; models are downloaded at install. The small-chip model is a fine-tune of
  `stories260K` (llama2.c, MIT).
- Font: IBM Plex Mono (SIL Open Font License).

## License

MIT. See [LICENSE](LICENSE).
