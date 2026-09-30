# Phase 0c, round 2: agent A (Pi bench, cards A2/A9, review items F5 and F8)

Branch `ws/a-bench`. Numbers and method: `docs/SPIKE.md`, "Round 2: 3-4B ladders and prefill".
Pi time: about 4.5 hours under the lock (10:48-15:24), one hold per spike, every long job a
detached unit. `get_throttled` was 0x0 on every run, 56.5 °C at most. No downloads.

## Headline for the integrator

1. **S4b (F5) is a GO: carry the KV cache across a reload.** The old server saves its slot to
   /dev/shm, the next quant restores it, and cache reuse absorbs the reload's cut. Measured
   from the reload's start to the first token: **Qwen3 1.7B 48 s instead of 109 s, Llama 3.2
   3B 73 s instead of 218 s** (the same prompt re-read on a fresh server). Save plus restore
   take 0.3 s for about 90 MB, and the new quant accepts the old one's cache. The thoughts after
   the restore name the new precision ("I can only think in 4-bit now"). Built into the backend
   behind `[backend] reload_handover = "slot" | "reread"`; the default stays `"reread"` until
   you decide (A15).
2. **F8: the S1c slowdown is the normal cost of a longer context.** Round 1's soak never
   trimmed its memory (Qwen3 was thinking, so every visible thought was empty and the
   character counter never filled), so its context grew to about 1,850 tokens. Re-run with
   real thoughts, `/slots` and the KV cache's high-water mark: seconds per token = 0.496 +
   0.000124 x the high-water mark (R² 0.99). With the memory capped, the speed holds at 1.51
   tokens/s from minute 7 to 30. The restart variant agrees: a restart only buys one or two
   faster thoughts. Budgeted as `tg_tok_s_late` at n_kv 1792 on the step-0 bench files.
3. **A plain 3-4B reload cannot fit 180 s**, prefill or not: load plus the system prompt alone
   is 127-173 s. With the slot handover it fits: 73 s measured, 78-105 s estimated.
4. **Which models pass `epitaph estimate` on measured costs** (`pi4/default`; details in SPIKE.md):

   | Model | re-read reloads | slot handover (A16 applied in memory) |
   |---|---|---|
   | Qwen3 1.7B | PASS, 39 thoughts, silences 128/154 s | PASS, 42 thoughts, silences 50/45 s |
   | Llama 3.2 3B | FAIL: silence 229/287 s, rules (a), (b) | FAIL only rule (c): no finished thought in the 55:00-57:00 erosion window; silences 84/79 s |
   | Qwen3 4B Instruct 2507 | FAIL: silence 337/381 s, (a)-(d) | FAIL: (a), (b), (c), (d); silences 105/97 s |
   | Phi-4-mini, SmolLM3, Gemma 3 4B | step 0 only on the Pi: not evidence (steps 1-2 use the overlay's estimates) | same |

   Speed decline passes wherever it is evaluated (0.22-0.35 against a limit of 0.40). No model
   fails *only* on silence with the re-read, so the recall-260/200 profile copy was tried
   (Llama: 219/288 s, no help) and not kept.
5. **F2 needs care for Llama 3.2 3B:** its step 1 at 3 threads generates 22% faster than
   step 0 (1.40 against 1.14 tokens/s at depth). To keep speed from rising at reload 1, the CPU
   share there must be at most about 2.4 cores, or the thread drop must move to reload 1
   (still +9%). Q2_K and Q3_K_M are compute-bound at 2 threads (Llama Q2_K 1.18 against 1.63).

## Built

- `tools/spike/s1_ladder.py`: a model's whole ladder at 3 and 2 threads (`-tb 3`, dio loads
  cold then warm, the system prefill, the birth thought, a deep re-read). Writes the
  `load_costs` format.
- `tools/spike/s4_reload.py`: the system prefill, dio loads, and the memory sized with the
  model's own template after the reload cut (recall x 0.85, whole turns).
- `tools/spike/s4b_slot_handover.py`: spike S4b, with real thoughts before and after the
  restore and a plain re-read control on the same prompt.
- `tools/spike/s1c_soak.py`, rewritten for F8: real thoughts, a token-counted rolling memory,
  `/slots` n_past and the KV high-water mark per thought, and `--restart-every-min` /
  `--compact-every-min` variants.
- `tools/spike/late_speed.py`: the context line from each file's birth and deep rates;
  `tg_ctx_model` on every file, and `tg_tok_s_late` / `late_after_s` on step-0 files.
- `tools/spike/llama.py`: thinking off on every request (round 1's spike requests left it on),
  `-tb`, `--load-mode`, prefill, exact token counts, slots, and KV marks.
  `tools/spike/summarize.py` prints the round-2 tables.
- `tools/spike/pi_run.sh`: never copies an older Pi result over a newer local file (the
  copy-back had overwritten the parked `bench/measured/` files with the Pi's stale copies;
  caught and restored before any commit).
- **Backend (F5):** `ServerSettings.reload_handover` / `slot_save_path`, `Handover` and
  `LlamaServerBackend.last_handover`. `start()` on a live creature of the same model saves,
  stops, loads and restores; the next `prefill` is skipped, because it would cut the restored
  cache back to the system prompt. Every failure falls back to the re-read, and a new model
  never inherits a cache. The fake models the same switch. The rehearsal clock charges the
  handover at S4b's Pi rate, so `epitaph rehearse --set backend.reload_handover="slot"` works.
- **Data:** `bench/measured/pi4-{llama-3.2-3b-instruct,qwen3-4b-instruct-2507}-{0,1,2}-{2,3}.json`,
  `pi4-{phi-4-mini-instruct,smollm3-3b,gemma-3-4b-it}-0-{2,3}.json` and the corrected Qwen3
  1.7B late speed in `bench/measured/`. Raw runs are in `bench/spike/`: `s1r2-*`,
  `s4-*-r2`, `s4b-*`, `s1c-*-r2*`. The machine's hostname is removed from every bench file.

## Tested

| Command | Result |
|---|---|
| `make check` | ruff and format clean, pyright 0 errors, **665 passed**, coverage 95.7%, sim and `estimate` pass on every Pi 4 profile |
| `tests/unit/test_llama_server.py` (4 new) | argv and settings, the handover carries the cache and skips one prefill, falls back on save/restore failures, only within one model |
| `tests/unit/test_backend_fake.py`, `test_rehearse.py` (1 new each) | the fake keeps its cache across a slot reload; a rehearsed life charges two handovers and no re-read |
| `tools/laptop_lock.sh run A 15 -- ... pytest -m model tests/templates -k slot` (Qwen3 1.7B Q8_0 → Q4_K_M) | **passed**: the real llama-server restores more than 300 tokens and the cut request reads fewer than 80 |
| Laptop S4b and S1c checks (scratch) | the cache crosses quants (60 of 465 tokens read); trims refill freed cells, so the high-water mark stays at the largest context |
| Pi: 5 ladder runs, 2 S4 runs, 2 S4b runs, 2 soaks | every row in SPIKE.md; `get_throttled` 0x0 throughout |

## Left

- **Adopt the round-2 costs (F9):** the files the profiles run on (`bench/pi4-qwen3-1.7b-*`)
  keep round 1's late value. On the round-2 files `pi4/default` has 39 thoughts instead of
  40 and still passes; `tests/unit/test_costmodel.py` expects at least 40, so move the bound
  when you adopt them.
- The cost model still charges the full re-read at a reload (A16), and it slows generation
  by time, not by context (A18). The estimate's statement "whether a reload resets the
  slowdown is not measured" is answered: it does, because the context is short again.
- Phi-4-mini, SmolLM3 and Gemma 3 4B have only step 0 on the Pi. If one is chosen, its lower
  quants (1.5-2 GB each) need downloading and a ladder run (about 55 minutes).
- The slot handover is untested with `--swa-full` (Gemma) and at reload 2 (Q4_K_M → Q2_K,
  2 threads); the mechanism is the same.
- The warm load was measured with `dio` only: equal to the cold load within 1 s. A warm mmap
  load is 4-9 s (round 1), but step 0 and step 1 of a 3-4B do not fit in RAM together, so a
  pre-read cannot help.
- The slot file sits in /dev/shm and is charged to the creature's cgroup. C: the controller
  unit must not hide /dev/shm (QUESTIONS A #16).

## Contract proposals (docs/process/CONTRACT_CHANGES.md, "Proposals in phase 0c, round 2")

- A15 `[backend] reload_handover = "slot"`, `slot_save_path` in `default.toml`.
- A16 the cost model's reload with a carried cache (load + handover_s + `reread_cost`).
- A17 the `prefill` docstring (may return 0 after a restore); the controller reloads with
  `start()` on the live creature, never `stop()` then `start()`.
- A18 generation speed by context (`tg_ctx_model`), not by time.

## Questions (docs/process/QUESTIONS.md, each with the default in use)

- A #13 load times are `dio`, cold; warm recorded alongside.
- A #14 `reload_handover` stays `"reread"` until A15.
- A #15 the late speed is taken at n_kv 1792, reached after 20 minutes.
- A #16 the slot file counts against the creature's memory for about a minute.
