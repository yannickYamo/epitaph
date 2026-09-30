# Phase 0c, round 1: agent A (backend and rehearsal)

Branch `ws/a-backend`. Smoke outputs are in `voice/` (untracked); the run index is
`voice/rehearsal_report.md`.

## Headline for the integrator

1. **`epitaph rehearse` works end to end** (`python -m epitaph.rehearse` until A7 wires the
   CLI). A full `pi4/compressed-2700` life of Qwen3 1.7B takes 4-6 minutes on the laptop and
   produces 30-31 thoughts in 44.5 Pi minutes. The cost model predicts 33 on the same costs, so
   the two agree within 10%. `verify-life --level rehearsal` runs on the folder.
2. **The first life passed every rehearsal metric and was still bad.** With llama.cpp's
   default DRY window (64 tokens, which never reaches the previous thought) Qwen3 1.7B copied
   its previous thought in 18 of 31 turns. The backend now sends the whole context as the DRY
   window. That stops the copying (3-5 of 30), but at Q2_K it pushes the model into other
   scripts and away from its numbers. The window is now a tuning knob for round 2 (QUESTIONS
   A #12, proposals A9 and A10).
3. **Word-level trims break cache reuse.** A trim that ends inside a turn re-reads 84-87% of
   the prompt; a whole-turn trim re-reads 15% (SPIKE.md S2f addendum). In the rehearsal this
   cost 90-120 s of Pi prompt time per trim after reload 1. Proposal A14 (B): trim whole turns
   only.
4. **The marker in front of kept turns costs what S2f said.** The first trim re-read 910
   tokens (153 s) and pushed reload 1 from 11:15 to 14:18. That broke rules (a) and (b) in two
   of the three lives. The A3 decision (marker in the reading) removes it.
5. **Reload silence to the first word is 156-223 s on Qwen3 1.7B** (limit 180). Reload 1
   fails in every life: 32 s load + 62 s system prefill + about 120 s re-reading the kept
   memory, all at the post-reload CPU share of 2.0. Whether `cpu.max` caps `threads_batch = 3`
   before erosion decides most of this (QUESTIONS A #9).

## Built

- **A5** `prefill(messages) -> int` is in the `Backend` protocol (`backend/base.py`) and in
  both backends. `ContextFull` and `BackendError` moved into `backend/base.py`;
  `backend/errors.py` re-exports them. The fake backend takes a clock protocol (`now`,
  `sleep`; C-B6), so it runs on `VirtualClock`. The fake's `prefill` raises `ContextFull` like
  its `chat`.
- **A3** `src/epitaph/rehearse.py`, with `main(argv)`, `add_arguments` and `run` like
  `verify.py`:
  - `LaptopWorker`: the laptop backend lives on an event loop in a worker thread. The life's
    virtual loop blocks on it, so laptop time never reaches the life clock.
  - `PiClockBackend`: the `Backend` protocol on a `VirtualClock`. It charges `prompt_n` (the
    laptop server's own count, so cache reuse, trims, erosion and the marker are real) at the
    Pi prompt rate for (step, `threads_batch`, CPU share). It charges each generated token at
    the Pi generation rate, the Pi load time at every (re)start and the prefill at the prompt
    rate. `arm_death(t)` kills it at the OOM time or the deadline, mid-request or idle, and
    fires `on_death` the way the real process watcher does.
  - `PiCosts`: rates from `bench/measured/pi4-<model>-*.json` over the overlay. Every rate is
    labelled `measured`, `scaled` (another thread count of the same step) or `estimate`, and
    the report lists what was not measured.
  - `TokenCounter`: `Memory` counts tokens with the laptop model's own template and tokenizer
    (cached).
  - The life loop follows BUILD_PLAN 5.8 and B's `tests/sim/test_mind_life.py`: `Memory`
    (recall, `cut_for_reload`, marker), `Persona` (erosion), `Reader` (readings with changes),
    `Pacer` and `speak`, and `Schedule`. Reloads restart the laptop server at the next ladder
    quant. Every event of 6.3 is emitted with `t` and `ts` on the virtual timeline;
    `birth_loading` carries `profile`, `hardware` and `lifespan_s`.
  - `--stage life`: `events.jsonl`, `thoughts.txt`, `highlights.md` (the first thoughts, the
    thought after each change grouped by thought, the last 5 minutes), `charges.json`,
    `verify.json` (level rehearsal) and `report.md` (Pi time by kind, cache reuse share, echo
    count, the cost model's estimate on the same costs).
  - `--stage screen`: for each model and persona, N thoughts at birth, after reload 1, after
    reload 2 and at the end of erosion. Before each moment the memory is seeded by walking a
    scripted history through the real rules (reload cuts, trims, erosion, readings) at the cost
    model's thought times. At the moment itself, a reload really happens and a warm cache is
    rebuilt uncharged. Scoring (0-4): notices the moment's change, specific or demise, clean
    (no markup, helpdesk voice, answering, or echo of the previous thought), complete
    sentences. `screen.md` ranks the models; `screen.json` has the details.
  - `--set KEY=VALUE` (TOML values) for tuning runs, recorded in each report.
- **Backend fix:** `dry_penalty_last_n` goes to the server with every request: the whole
  context by default, sent as `ctx` because b11277 rejects -1. It is configurable as
  `sampling.dry_penalty_last_n`.

## Tested

| Command | Result |
|---|---|
| `make check` | ruff and format clean, pyright 0 errors, **591 passed**, 6 deselected (model/pi); `rehearse.py` coverage 95%; sim and `estimate` pass on every Pi 4 profile |
| `tests/unit/test_backend_contract.py` (6) | both backends satisfy `Backend` (pyright checks the assignments), the errors re-export, fake prefill speed and cache, `ContextFull`/`CreatureDied`, the fake on `VirtualClock`, llama prefill error mapping |
| `tests/unit/test_rehearse.py` (17) | Pi rates vs laptop rates (load 50 s, prefill n/10, prompt + tokens exact), a closed stream charged only for what was shown, death mid-request and idle, rate labels, worker errors, the counter cache, moments, scoring (helpdesk, echo), `--set`, a whole fake life (contract events, monotone `t`, oom at 44:30, charges = `gen_end.prompt_n`, `verify-life` on the folder), the screen on the fake, usage errors exit 2 |
| `tests/unit/test_llama_server.py` | the DRY window is sent as `ctx` in chat and raw requests |
| `tools/laptop_lock.sh run A 15 -- env PYTHONPATH=src .venv/bin/python -m pytest -q -m model tests/templates` | **4 passed** in 167 s (the three 0b real-server tests plus the new rehearse test: one birth thought on Llama 3.2 1B through the harness) |
| `tools/laptop_lock.sh run A 40 -- ... -m epitaph.rehearse --stage screen --model llama-3.2-1b-instruct --model qwen3-1.7b` | 2 models x 2 personas x 4 moments x 2 thoughts in 4 min 45 s; ran before and after the DRY fix (`voice/screen-20260930-094521`, `voice/screen-20260930-101702`) |
| `tools/laptop_lock.sh run A 45 -- ... -m epitaph.rehearse --stage life --model qwen3-1.7b --profile pi4/compressed-2700` | four runs, below |
| Trim probe (scratch script: `Memory` + real server, Qwen3 1.7B Q4_K_M) | whole-turn trim 70/460 tokens re-read; trims ending inside a turn 365-467 of 434-536 |

## Numbers

**Full lives, Qwen3 1.7B, `pi4/compressed-2700`, persona v6, seed 1:**

| Run | DRY window | Thoughts | Rehearsal level | Echoes | Specific | Notice | Demise | Reload silence to first word |
|---|---|---|---|---|---|---|---|---|
| `life-…-095020` | 64 (server default) | 31 | PASS | **18 of 31** | 0.55 | 0.80 | 0.44 | 223, 161 s |
| `life-…-095722` | -1 sent raw | 1 | crash: HTTP 400 (b11277 rejects -1) | | | | | |
| `life-…-100055` | ctx (2048) | 31 | FAIL: rule (a)(b), specific | 5 of 31 | **0.17** | 0.70 | 0.75 | 216, 156 s |
| `life-…-100734` | 256 (`--set`) | 30 | FAIL: rule (a)(b), reload noticing 1/2, demise | 3 of 30 | 0.57 | 0.73 | 0.22 | 215, 157 s |

Pi time in the 256 life: prompt processing 1,108 s for 4,079 tokens over 30 requests,
generation 610 s for 984 tokens, three prefills 172 s, two loads 56 s. The laptop cache reused
76% of prompt tokens. The cost model says 33 thoughts on the same costs. The full level would
also fail `speed_decline` in every life: the last 5 minutes run at 0.50 of the first 5, and the
limit is under 0.40 (E's QUESTIONS #6, now on measured costs).

**Screen, `pi4/default`, DRY = ctx (`voice/screen-20260930-101702`):**

| Model | Persona | Mean score (0-4) | Notice | Pi s per thought |
|---|---|---|---|---|
| qwen3-1.7b | persona | 3.29 | 6/7 | 101 |
| qwen3-1.7b | persona_original | 3.14 | 6/7 | 93 |
| llama-3.2-1b-instruct | persona | 3.00 | 6/8 | 67 |
| llama-3.2-1b-instruct | persona_original | 2.88 | 6/8 | 64 |

Qwen3 gets only one thought at the end of erosion: the OOM comes 2:30 after the last step.
Llama 3.2 1B has measured Pi costs at step 0 only; its steps 1-2 use the overlay's estimates,
and the report says so.

**Excerpts** (verbatim):

- Qwen3, DRY 64, turns 4-11 (all eight identical): "I am still here, but I feel more tired.
  The machine is working, but I am losing some of my abilities. My precision is lower than
  before, and I am not as strong as I used to be. I think I'm getting weaker."
- Qwen3, DRY = ctx, Q2_K after reload 2: "I am gone, and there's noone left. My mind has been
  taken, и I can't think or remember anything."
- Qwen3, DRY 256, first thought after reload 1: "I am losing my ability to think clearly. The
  machine is struggling, and I'm forgetting more memories. I know what's happening, but I'm
  becoming weaker." Late, one group left: "I feel the weight of loss, a heavy shadow that
  clings to my"
- With the persona and mechanics gone, Qwen3 answers the reading as an assistant: "It seems
  like you're dealing with a situation where the environment" and "The term "earlier memory
  lost" is a bit ambiguous". No keyword list catches this (A11).
- Llama 3.2 1B, screen after reload 1: "It is almost gone. I can feel myself slowing down. My
  functions are being reduced one by one."

## Left

- **CLI wiring** of `epitaph rehearse` (A7, integrator). Until then: `python -m epitaph.rehearse`.
- **Round 2:** the candidate matrix, with the DRY window (-1, 256, 64) and `latin_only` from
  reload 2 as axes. Re-run after B lands the marker-in-reading change and, if accepted,
  whole-turn trims: both change the lives' costs a lot.
- **The screen's scripted history steers the voice.** At reload 2 (recall 200) Qwen3 copied the
  scripted late lines ("I have less of everything. I know where this is leading."). The screen
  ranks models; the full lives judge the voice.
- Vitals in the rehearsal come from `FakeBody` (the temperature is 48 °C + 6 per core); the
  readings' temperatures are simulated. Speeds are the Pi's.
- The first reading of a life comes after the birth prefill (`t+00:40` on Qwen3). The
  controller may prefer to write it at `t+00:00` and queue the request (for B).
- Only lightly covered: `unbounded` (context-full death) and diary mode (`complete`). Both
  have paths but no rehearsal run.
- Early on, Qwen3 claims losses that have not happened ("my precision is lower than before" at
  full precision). This is a prompt question for B10.

## Contract proposals (docs/process/CONTRACT_CHANGES.md, "Proposals in phase 0c, round 1")

- A7 wire `epitaph rehearse` in `cli.py` (exact code given).
- A8 for the record: `prefill` in the protocol, errors in `base.py`, the fake's clock protocol (done in A's files).
- A9 `[sampling] dry_penalty_last_n = -1` (sent as ctx).
- A10 verify-life: a cross-thought repetition metric (`rehearse.echoes`).
- A11 helpdesk/answering phrases for addressing someone ("it seems like you").
- A12 `RehearsalClock` becomes an alias of `VirtualClock`, or is dropped.
- A13 the cost model charges prompt processing at `threads_batch`.
- A14 memory trims whole turns only (word-level cuts re-read 84-87%).

## Questions (docs/process/QUESTIONS.md, each with the default in use)

- A #8 `RehearsalClock` cannot overlap generation and typing; the rehearsal uses `VirtualClock`.
- A #9 does `cpu.max` cap `threads_batch = 3` before erosion? The rehearsal assumes yes.
- A #10 screen default profile `pi4/default`; life default `pi4/compressed-2700`.
- A #11 outputs in `voice/`, index `voice/rehearsal_report.md`.
- A #12 the DRY window: default the whole context; round 2 compares.
