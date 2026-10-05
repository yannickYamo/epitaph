# epitaph on small chips

**The installation needs a Raspberry Pi and a 4-billion-parameter model. This folder shows the
same piece on chips a thousand times smaller: a 260,000-parameter model that lives, loses its
machine and dies on a $30 badge or a $5 ESP32. The prompt cannot shrink that far, so the voice
moves into the weights.**

| | Raspberry Pi 4 (the installation) | Tufty 2350 badge | ESP32 |
|---|---|---|---|
| Chip | 4-core Cortex-A72, 4 GB | RP2350, 8 MB PSRAM | Xtensa LX6, 520 KB SRAM |
| Model | Qwen3 4B, 4-bit, 2.5 GB | 260K, float32, 1 MB | 260K, int8, 260 KB in flash |
| How it gets its voice | the prompt | taught by Qwen3 4B under that prompt | the same weights |
| What it loses | services, radio, light, screen, CPU, clock, memory, RAM | light, memory window, clock (250 to 48 MHz), screen, RAM | light, memory window, clock (240 to 80 MHz), heap; a screen if the board has one |
| Speed | about 1 token a second | about 8 tokens a second at 250 MHz | not yet measured on hardware |
| Status | running | an earlier build ran on the badge; this one runs in simulation | runs in a simulated ESP32 on a laptop (below); compiles for an ESP32 and a Pico |

## The voice moves into the weights

**A model this small cannot read instructions, so it is taught the voice instead.**

The smallest models that follow a persona prompt have about 100 million parameters, roughly
400 times what an ESP32 can hold. So the voice is distilled:

1. **The teacher writes.** Qwen3 4B, with the installation's exact prompt
   (`config/default.toml`), lives badge lives: its light switched off, its memory cut, its clock
   lowered, its screen dimmed, its RAM taken ([`tools/teach.py`](tools/teach.py),
   [`data/lives.jsonl`](data/lives.jsonl)). The real thoughts of the Pi's lives are added
   ([`data/pi_thoughts.txt`](data/pi_thoughts.txt)).
2. **The student learns.** Karpathy's open `stories260K` (llama2.c, MIT), trained on children's
   stories, is fine-tuned on that text for 200 steps on a laptop CPU
   ([`tools/finetune.py`](tools/finetune.py)).
3. **Real words only.** At each step the chip lets the model choose only among pieces that build
   words of its training text that a dictionary also knows
   ([`tools/build_lexicon.py`](tools/build_lexicon.py)). The model still chooses every word;
   it can no longer misspell one.

The machine's readings are fed to the model, as on the Pi, but not shown. A loss is read at
the model's next full stop, and losses that fall together are read as one reading. A terminal face
before each paragraph shows the machine's state instead: `(o_o)` awake, `(._.)` in the dark,
`(o_O) ...` forgetting, `(-_-) zzz` slowing, `(;_;)` dimming, `(x_x)` dying.

The trade is plain. The Pi's model reasons about what it senses; this one has its grammar
loosened by its size, and a few hours more of teaching would tighten it. What survives is the
subject, the losses and the death. The prompt became the training set.

## Every loss is real

**The same rule as the installation: a reading reports only something the chip just did.**

- **Light:** the board's LED, or the badge's rear lights, are switched off.
- **Memory:** attention is limited to the last N positions, and the reading quotes the start of
  what fell out. 128 positions at birth on an ESP32, then a third, an eighth, a twelfth and a
  sixteenth.
- **Clock:** the CPU frequency is lowered for real; the reading reports the speed it measures.
  An ESP32 stops at 80 MHz, where it still keeps its serial port.
- **RAM:** at 96% of the life the chip takes its heap in large bites, all but the room for one
  more thought. The model reads `your memory is being taken` and answers in a few words. Then
  the rest is taken, the next thought cannot be allocated, and that failed allocation is the
  death.
- **What a board cannot do is never said.** A board with no LED, no clock control or no
  backlight skips those steps, and no reading is written for them.

A life's losses are spread over ten minutes. The death follows its last answer: a few seconds
later on an ESP32, about two minutes later on the badge, whose clock is by then at a fifth of
its speed (a deadline ends the life at three). A whole life is about twelve minutes.

## Run the ESP32 version

```sh
make -C badge/esp32 life       # a whole life on a simulated ESP32: a 300 KB heap, a clock at
                               # the chip's speed; prints the words, the losses and the death
badge/esp32/host/test_host bare    # the same life on a board with only a serial port
python badge/tools/test_esp32.py   # the int8 C engine against the float model
python badge/tools/test_ports.py   # both ports read the readings as the model was taught them
```

On a board: open [`esp32/epitaph_esp32/epitaph_esp32.ino`](esp32/epitaph_esp32/epitaph_esp32.ino)
in the Arduino IDE (board "ESP32 Dev Module"), upload, and open the serial monitor at 115200
baud. Any ESP32 with 4 MB of flash works; no PSRAM is needed.

| What we checked | Result |
|---|---|
| int8 C engine against the float32 model | same next token 39 of 40 steps; logits within 2.5% of their range |
| A whole simulated life (300 KB heap) | the memory window takes 164 KB; every loss the board can perform lands on time and is read once; the last reading is read and answered; the heap squeeze kills it; everything is returned at the death |
| The same life on a board with only a serial port | the light, clock and screen steps are skipped and never reported; it still forgets and dies of memory |
| Readings as the model reads them | 38 readings encode to the token ids of the training, on both ports |
| Compiled for a real ESP32 (arduino-cli, esp32 core 3.3.12, ESP32 Dev Module) | 578 KB of 1.3 MB flash; 24 KB of static RAM, leaving 304 KB of heap |
| Compiled for a Raspberry Pi Pico (arduino-cli, mbed_rp2040 core) | 403 KB of flash; 45 KB of static RAM, leaving 226 KB |
| Run on a real ESP32 or Pico | not yet: no board was at hand |

## Run the Tufty version

Double-tap RESET (the badge mounts as `TUFTY`), copy [`tufty/epitaph`](tufty/epitaph) to
`apps/`, add `epitaph` to `apps/menu/order.txt`, eject, press RESET and choose **Epitaph**.
`python badge/tools/sim_badge.py` runs the app on a laptop on a virtual clock, with a heap the
badge can run out of; `--check` fails unless the life ends as it must.

The badge last ran an earlier build. This one reads each loss sooner and answers the last
reading before it dies; it passes the simulated life and has not yet run on the badge.

## Port it to another board

The life knows no board. On a microcontroller a port fills in four required hooks in C, or
one small class in MicroPython, and says what its hardware can take; what it cannot is skipped.
[`terminal.py`](tufty/epitaph/terminal.py) is the smallest port: it runs on any MicroPython
board with `ulab`, and on a laptop with `python badge/tufty/epitaph/terminal.py 60 1`.
[docs/PORTING.md](../docs/PORTING.md) has the hooks, the RAM each board needs and the checks.

## Rebuild the model

```sh
pip install numpy torch
python badge/tools/teach.py --lives 50 --out badge/data/lives.jsonl     # llama-server with Qwen3 4B on :8099
python badge/tools/finetune.py stories260K.bin tok512.bin badge/data/lives.jsonl out.bin badge/data/pi_thoughts.txt
python badge/tools/convert_tinyllama.py out.bin tok512.bin badge/tufty/epitaph/assets
python badge/tools/build_lexicon.py badge/data/lives.jsonl badge/data/pi_thoughts.txt badge/tufty/epitaph/assets/lexicon.json
python badge/tools/export_esp32.py badge/tufty/epitaph/assets badge/esp32/epitaph_esp32/model_data.h
```

`stories260K.bin` and `tok512.bin` come from
[karpathy/tinyllamas](https://huggingface.co/karpathy/tinyllamas) (MIT). The fine-tuned weights
in this folder (1 MB, and 260 KB as int8) are a derivative of that model, under the same license.
