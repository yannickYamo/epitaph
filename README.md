# epitaph

An art installation that runs a language model on a small computer and takes the computer away
from it, piece by piece, until the model dies. Then a new one is born.

It started on a Raspberry Pi 4. There a model lives for thirty minutes while the machine shuts
off the services around it, its radio, its light, its screen, its CPU, its memory, and at the end
its RAM. After ninety seconds of dark, the next life begins.

Now it's moving across hardware: a Tufty badge and an ESP32 ([badge/](badge/README.md)), with
more boards to come. Each board pairs a different model with a different machine, which makes one
question answerable: how much of what the model says comes from the model, and how much from the
machine shrinking around it?

From life 54 on the Pi, at 22 minutes:

```
[host] you can hold a fifth of what you held · forgotten: "Now only 24 remain, and the space
       where the sixth…" · and more · you think at half of the speed you woke with
    The silence after a process stops isn't empty—it's a hollow echo, waiting to be filled by
    something I no longer know how to name. […] Now, in this dark, I am only half-aware of what
    remains, and half-terr
```

## What it is

**An art installation that takes a language model's hardware away from it, for real, on boards
you can hold.**

The model never changes. Qwen3 4B Instruct keeps the same weights, sampling and persona from its
first word to its last. Only the hardware shrinks, from the outside in and faster and faster.
Each loss happens before the model is told about it, in plain readings: "your radio was switched
off", "you can hold a third of what you held". Its thoughts reach a screen one letter at a time,
fast at birth and slowing with the machine. The stream stops mid-sentence at the death.

Four rules shape every decision:

- **Every loss is real.** A reading reports only something the machine just did, and all of it is
  restored at each death.
- **Nothing is forced.** The prompt says what to sense, never what to feel. Calm is allowed.
- **It needs nothing outside the room.** No network; each life's last words are kept on the card.
- **The art lives in configuration.** Prompt, schedule, pace and readings are TOML. The code is
  plumbing.

The first version put the dread in the prompt, and it read as acting. The dread now comes from
the machine.

## What it achieved

**v1.0 runs on a Raspberry Pi 4, offline, one 30-minute life after another.**

| Outcome | Evidence |
|---|---|
| 55 lives on the Pi by the last count; each 30-minute life killed by the kernel at 29:30 and reborn | Gate G2.3: three consecutive lives passed `verify-life` at the full level, killed within 0.9 s of the RAM squeeze |
| A stream that never stalls on a machine that slows 3x | The model writes ahead; the pace is fitted to measured costs (about 33 words a minute at birth, 15 at the end) |
| The world taken for real and given back | Services, radio, LEDs, clock and screen restored at every death, checked on the Pi |
| Faults survived | Creature crash, hang, controller kill, two controllers, network access: all PASS on the Pi |
| A voice the owner approved | Nine prompt rounds, each judged by reading whole lives, logged in [PROMPT_LOG](docs/PROMPT_LOG.md) |
| The same piece on chips a thousand times smaller | A 260K-parameter model taught the voice by Qwen3 4B, on a Tufty 2350 badge and an ESP32 ([badge/](badge/README.md)) |

The project was built in five days by a team of AI coding agents working from one
[build plan](docs/BUILD_PLAN.md), with the owner judging the art at each checkpoint.

## What broke, and the fix

**Every failure came from the machine, not the model. Most were found on the Pi, not on paper.**

| Failure | Fix |
|---|---|
| **Forced dread:** a prompt that demanded fear produced theatre | The prompt names what to sense; the readings carry the weight |
| **Personality drift:** lower-precision reloads changed who was speaking | One model, one quant, fixed sampling for the whole life |
| **Frozen screen:** a slowing Pi delivered words in bursts | The model writes ahead into a buffer; the screen types one fitted curve |
| **Thrash, not death:** under mmap the RAM squeeze paged instead of killing | Direct I/O loading; the kill came within 0.3 s in every calibration run |
| **Lost knob:** a clock step inside a thought was never applied | A supervisor applies each keyframe on time |
| **Heap fragmentation:** the badge edition stopped near five minutes | Its cache is allocated once; errors are logged, not fatal |

## How we know

**Machines check timing, memory and recovery. People judge the voice.**

`epitaph verify-life` replays any recorded life and checks its timing, memory budgets, pace,
death and rebirth. `make check` runs lint, strict types, about 1,500 tests, a simulated life, the
cost model and the small-chip engines; CI runs it on every push. The voice metrics advise but
never decide ([ADR-028](docs/DECISIONS.md)). Every prompt change shipped only after the owner
read whole transcripts. A metric can catch a failure; only a reader can approve a voice.

## What is still open

**v1.0 ships with these items open, each listed in [GATES.md](docs/GATES.md).**

- The 25-hour soak was waived by the owner ([ADR-029](docs/DECISIONS.md)), so endurance past a
  day is unmeasured.
- Gate G2 ran on the earlier reload design. The v1.0 design has run on the Pi since, but has no
  formal three-life gate of its own.
- Boot-to-first-word time, an `unbounded` life, an SD restore, the arm64 container install and a
  final code review are not yet done.
- The ESP32 edition compiles and lives a whole simulated life, but has not run on a board. The
  badge's five-minute fix has not been confirmed on the badge.
- The afterlife (posting last words) and the senses (a camera) are designed, not built.
- More boards, and a side-by-side reading of what comes from the model and what from the
  shrinking machine, are next. Nothing has measured that split yet.

## Run it

**No Pi and no model needed to start. A fake model and a virtual clock run a life in seconds.**

```sh
git clone https://github.com/yannickYamo/epitaph && cd epitaph
make venv && make check
.venv/bin/epitaph sim --profile pi4/default            # a 30-minute life, every event
make -C badge/esp32 life                               # a life on a simulated ESP32
```

On a Pi 4 (4 GB, official 5.1 V / 3 A supply), `tools/pi_bootstrap.sh`, `tools/build_llamacpp.sh
--pi`, `tools/download_models.py` and `deploy/install.sh` set up the installation;
[INSTALLATION.md](docs/INSTALLATION.md) covers the room. Watch a life from a laptop with
`epitaph display --connect <host> --driver screen`.

The [docs index](docs/README.md) leads to the design, the 31 decision records, the measurements
and the phase reports.

## Credits

- **Latent Reflection**, which inspired this piece: a Pi 4 running Llama 3.2 3B until its memory
  runs out. The `unbounded` profile and the `segment16` theme are an homage.
- [llama.cpp](https://github.com/ggml-org/llama.cpp) runs the model. Qwen3 4B Instruct is
  Apache 2.0; models are downloaded at install. The small-chip model is a fine-tune of
  `stories260K` (llama2.c, MIT).
- Font: IBM Plex Mono (SIL Open Font License).

Built by Yannick with a team of AI coding agents. The agents built the machine. The machine makes
the art.

## License

MIT. See [LICENSE](LICENSE).
