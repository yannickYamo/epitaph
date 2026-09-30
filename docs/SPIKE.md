# Spike results

Each spike records its numbers and a go or fallback decision (BUILD_PLAN 8.5). Raw results:
`bench/spike/*.json` (per run), `bench/measured/pi4-*.json` (Pi costs in the `costmodel.load_costs`
format, parked; see "Using the numbers"), `bench/dev-*.json` (laptop costs). Tables are printed by
`python3 tools/spike/summarize.py`.

## part A (backend): S6, S2f, S1a, S1b, S2t, S4, S1c

Setup for every run: llama.cpp **b11277** (commit eae11d2), built natively on each machine
(`tools/build_llamacpp.sh`, `-mcpu=cortex-a72+crc+nodotprod+noi8mm` on the Pi);
`llama-server -c 2048 -np 1 --cache-ram 0 --jinja -ngl 0`, `--cache-reuse 256` unless stated;
on the Pi `taskset -c 1-3`, 3 threads, the official 5.1 V / 3 A supply, `get_throttled=0x0`
throughout unless stated.

### Upstream changes that matter at b11277

- **`--no-mmap` is gone** ("error: invalid argument"); it is now `--load-mode none` (`-lm none`).
  Checked on the laptop: with `none` the weights are anonymous memory (RssAnon 868 MB, RssFile
  16 MB for the 1B Q4_K_M), with mmap they are file pages. Anyone starting llama-server by hand
  (S3) must use `-lm none`.
- **`--cache-ram` defaults to 8192 MiB** (host-RAM prompt cache). The backend passes `--cache-ram 0`.
- `-np` defaults to auto (several slots): the backend passes `-np 1` so one slot owns the 2048 ctx.
- `max_tokens: 0` still generates one token; prefill uses 1.

### S6: templates and parameters (laptop, every candidate's step 0) — GO

| Model | system kept | 2 user turns | count exact | overhead/msg | DRY | grammar | prefill | raw | hygiene |
|---|---|---|---|---|---|---|---|---|---|
| gemma-3-1b-it Q8_0 | yes | no | yes (413 vs 414) | 5.0 | yes | yes | yes | yes | echoes_host, markup |
| gemma-3-4b-it Q4_K_M | yes | no | yes (413 vs 414) | 5.0 | yes | yes | yes | yes | clean |
| llama-3.2-1b-instruct Q8_0 | yes | yes | yes (412 vs 413) | 5.0 | yes | yes | yes | yes | markup |
| llama-3.2-3b-instruct Q6_K | yes | yes | yes (412 vs 413) | 5.0 | yes | yes | yes | yes | clean |
| phi-4-mini-instruct Q4_K_M | yes | yes | yes (371 vs 371) | 2.0 | yes | yes | yes | yes | clean |
| qwen3-1.7b Q8_0 | yes | yes | yes (410 vs 410) | 5.0 | yes | yes | yes | yes | clean |
| qwen3-4b-instruct-2507 Q4_K_M | yes | yes | yes (406 vs 406) | 5.0 | yes | yes | yes | yes | clean |
| smollm3-3b Q6_K | yes | yes | yes (437 vs 437) | 7.0 | yes | no | yes | yes | clean |

- **Token counting:** render with the model's template (`/apply-template`) and `/tokenize`
  matches the server's own prompt count within 1 token for every model. `count_past_tokens` =
  tokens(render(past + probe)) − tokens(render(probe)). Template overhead is 5 tokens per
  message (Phi 2, SmolLM3 7), so the content-only count undercounts by about 15%.
- **Thinking:** Qwen3 1.7B and SmolLM3 take `chat_template_kwargs = {"enable_thinking": false}`
  (SmolLM3 then renders an empty think block into the prompt; its output has none). The backend
  sends the switch on every request; templates without it ignore it.
- **Two user turns in a row are rejected by Gemma 3** ("Conversation roles must alternate"):
  the memory-gap marker cannot be its own user message. Everything else accepts it.
- DRY, repeat penalty, min_p, seed, GBNF grammar (so `latin_only` can be a grammar), assistant
  prefill (the rendered prompt ends open) and raw `/completion` work everywhere. SmolLM3's output
  broke the test grammar once (a character outside the set); not a blocker.
- **Voice hygiene in three plain thoughts:** Gemma 3 1B wrote the next `[host]` reading itself and
  used markup; Llama 3.2 1B used markup. The 3-4B models and Qwen3 1.7B were clean.

### S2f: does cache reuse survive the controller's edits? (laptop) — GO, with one design change

`tools/spike/s2_cache_reuse.py`: a life-like chat filled to about 1,000 tokens of memory with real
generations, then each edit the controller makes; `timings.prompt_n` is what the server really
re-read. "Share" = re-read tokens ÷ prompt tokens.

| Run | Model | reuse | first marker | warm trim | erosion | late trim (tokens) | late erosion | reload re-read | works |
|---|---|---|---|---|---|---|---|---|---|
| dev-gemma-3-1b-it-swafull | gemma-3-1b-it Q8_0 | 256 swa-full | 80% | 4% | 4% | 9% (55) | 9% | 705 tok | yes |
| dev-gemma-3-1b-it | gemma-3-1b-it Q8_0 | 256 | 100% | 100% | 100% | 100% (597) | 87% | 675 tok | no |
| dev-gemma-3-4b-it-swafull | gemma-3-4b-it Q4_K_M | 256 swa-full | 80% | 5% | 4% | 9% (55) | 9% | 734 tok | yes |
| dev-gemma-3-4b-it | gemma-3-4b-it Q4_K_M | 256 | 100% | 100% | 100% | 100% (597) | 87% | 676 tok | no |
| dev-llama-3.2-1b-instruct | llama-3.2-1b-instruct Q8_0 | 256 | 82% | 3% | 3% | 38% (152) | 11% | 528 tok | yes |
| dev-llama-3.2-3b-instruct-marker-reading | llama-3.2-3b-instruct Q6_K | 256 | 4% | 3% | 3% | 49% (230) | 9% | 599 tok | yes |
| dev-llama-3.2-3b-instruct-noreuse | llama-3.2-3b-instruct Q6_K | 0 | 82% | 82% | 91% | 38% (150) | 74% | 518 tok | no |
| dev-llama-3.2-3b-instruct | llama-3.2-3b-instruct Q6_K | 256 | 82% | 3% | 3% | 38% (152) | 11% | 528 tok | yes |
| dev-phi-4-mini-instruct | phi-4-mini-instruct Q4_K_M | 256 | 83% | 3% | 2% | 39% (143) | 10% | 480 tok | yes |
| dev-qwen3-1.7b | qwen3-1.7b Q8_0 | 256 | 82% | 7% | 7% | 51% (236) | 18% | 600 tok | yes |
| dev-qwen3-4b-instruct-2507 | qwen3-4b-instruct-2507 Q4_K_M | 256 | 82% | 4% | 3% | 40% (151) | 12% | 515 tok | yes |
| dev-smollm3-3b | smollm3-3b Q6_K | 256 | 80% | 4% | 3% | 48% (245) | 9% | 641 tok | yes |

Findings:

1. **Warm front trims and erosion steps are cheap with `--cache-reuse 256`** (2-7% re-read) for
   every candidate. Without it they re-read 82-91%. `trim_to = 0.85` stands; `cache_reuse = 256`
   stays.
2. **Gemma 3 needs `--swa-full`**: without it nothing is ever reused (100%); with it, like the
   others. `swa_full = "auto"` with `sliding_window = true` in models.toml is right. It costs KV
   RAM (see S1a).
3. **The first memory-gap marker breaks reuse when it is inserted in front of the kept turns**
   (80-83% re-read, about 1,100 tokens). llama-server's reuse scan only moves forward in the new
   prompt on a match, so any new text before kept text makes everything after it a re-read. At
   Pi prompt speeds (S1b: 2-3 tokens/s for 3-4B) that is a 6-8 minute silence. A marker that
   later moves to the new oldest turn is fine (3-4%), and so is removed text.
   **Fix, measured:** append the marker to the reading after the first loss instead
   (`--marker reading`: 4%). Proposed to B (CONTRACT_CHANGES #3).
4. The same rule applies to every other edit: the system prompt must be **rebuilt from the kept
   groups** (`"\n\n".join(kept)`), never cut with a string replace. A replace left a new run of
   newlines and re-read 92% in the real-server test (`tests/templates`); the rebuilt prompt stays under the 25% bound there and at 2-4% in S2f.
5. A cut from about 1,000 to about 170 tokens of memory (the late settings) re-reads 150-250
   tokens (38-51%): the kept tail is below the 256-token reuse chunk. Cheap in absolute terms.
6. `cache_reuse_works = true` for every candidate except Gemma without `--swa-full`.

### S1a and S1b on the Pi

Step 0 of every candidate, 3 threads, cold load (page cache dropped). "Birth thought" is measured
end to end: system prompt (about 250 tokens) + first reading + 70 generated tokens. "pp" is the
prompt speed over a 700-token re-read (1,300 for Llama 3B Q6_K); "tg" is generation at birth and
after that re-read. Headroom = MemAvailable minus the server's file-backed RSS (the weights in the
page cache), the minimum over the run; with `--load-mode none` MemAvailable itself.

| Model | quant | thr | mmap | load s | birth thought s | pp tok/s | tg tok/s (birth / depth) | headroom MB | fits | throttled |
|---|---|---|---|---|---|---|---|---|---|---|
| gemma-3-1b-it | Q8_0 | 3 | mmap | 27.7 | 43.6 | 11.99 | 3.233 / 3.132 | 2385 | yes | 0x0 |
| gemma-3-4b-it | Q4_K_M | 3 | mmap | 60.7 | 150.8 | 2.64 | 1.37 / 1.244 | 589 | yes | 0x0 |
| llama-3.2-1b-instruct | Q8_0 | 3 | mmap | 33.2 | 56.6 | 9.36 | 2.617 / 2.231 | 2128 | yes | 0x0 |
| llama-3.2-3b-instruct | Q6_K | 3 | mmap | 63.9 | 175.9 | 2.31 | 1.275 / 1.018 | 645 | yes | 0x0 |
| phi-4-mini-instruct | Q4_K_M | 3 | mmap | 61.3 | 147.9 | 2.59 | 1.339 / 1.176 | 811 | yes | 0x0 |
| qwen3-1.7b | Q8_0 | 3 | mmap | 52.2 | 81.2 | 5.96 | 1.84 / 1.653 | 1183 | yes | 0x0 |
| qwen3-4b-instruct-2507 | Q4_K_M | 3 | mmap | 60.5 | 165.2 | 2.36 | 1.261 / 1.047 | 774 | yes | 0x0 |
| smollm3-3b | Q6_K | 3 | mmap | 61.1 | 175.6 | 2.37 | 1.367 / 1.183 | 872 | yes | 0x0 |
| gemma-3-1b-it | Q8_0 | 3 | none | 27.2 |  | 12.43 |  /  | 2393 | yes | 0x0 |
| gemma-3-4b-it | Q4_K_M | 3 | none | 60.0 |  | 2.74 |  /  | 702 | yes | 0x0 |
| llama-3.2-1b-instruct | Q8_0 | 3 | none | 32.6 |  | 9.69 |  /  | 2108 | yes | 0x0 |
| llama-3.2-3b-instruct | Q6_K | 3 | none | 63.8 |  | 2.44 |  /  | 698 | yes | 0x0 |
| phi-4-mini-instruct | Q4_K_M | 3 | none | 60.0 |  | 2.72 |  /  | 801 | yes | 0x0 |
| qwen3-1.7b | Q8_0 | 3 | none | 51.5 |  | 6.33 |  /  | 1167 | yes | 0x0 |
| qwen3-4b-instruct-2507 | Q4_K_M | 3 | none | 60.0 |  | 2.42 |  /  | 773 | yes | 0x0 |
| smollm3-3b | Q6_K | 3 | none | 61.4 |  | 2.4 |  /  | 863 | yes | 0x0 |

`llama-bench` (pp128, tg32, 3 threads, warm) for the other quants:

| Model file | pp128 tok/s | tg32 tok/s |
|---|---|---|
| llama-3.2-3b-instruct/Q4_0 | 3.19 | 1.92 |
| llama-3.2-3b-instruct/Q4_K_M | 3.35 | 1.83 |
| llama-3.2-3b-instruct/Q2_K | 2.98 | 2.22 |
| qwen3-4b-instruct-2507/Q4_0 | 2.43 | 1.54 |
| qwen3-1.7b/Q8_0 | 6.55 | 2.02 |
| llama-3.2-1b-instruct/Q8_0 | 9.90 | 2.81 |
| llama-3.2-1b-instruct/Q4_K_M | 9.89 | 4.56 |
| gemma-3-1b-it/Q8_0 | 13.19 | 3.43 |

**S1a (fit): GO for every candidate.** Every step-0 file fits at ctx 2048 with f16 KV in both
load modes, with 589-2,393 MB headroom (the tightest: Gemma 3 4B Q4_K_M with mmap, 589 MB;
with `--swa-full` see batch 2 below). Anonymous memory with mmap is small (170-600 MB: KV, compute
buffers, repacked tensors); with `--load-mode none` the whole model is anonymous (2.8 GB for the
3-4B). `q8_0` KV is not needed.

**S1b (speed): prompt processing is the Pi 4's bottleneck, 3-5x slower than the estimates.**

- 3-4B models: pp 2.3-2.7 tokens/s (estimate: 10), tg 1.0-1.4 tokens/s (estimate: 1.35; Latent
  Reflection's 1.38 matches). Birth thought 148-176 s: **fails the 90 s go**. The system prompt
  alone is 80-107 s of it.
- Qwen3 1.7B Q8_0: pp 6.0, tg 1.65-1.84; birth thought 81 s: **go**.
- Llama 3.2 1B and Gemma 3 1B: pp 9.4-12, tg 2.2-3.2; birth 44-57 s: go (but S6 hygiene flags).
- Q4_0 is not faster than Q4_K_M on the A72 (Llama 3B pp 3.19 vs 3.35, tg 1.92 vs 1.83): no
  dot-product instructions, so the Q4_0 repack gains nothing. Lower quants barely help pp (Q2_K
  2.98): prompt speed is compute-bound, generation is memory-bound.
- Thermal and power: `get_throttled=0x0` on every run, 51-55 °C.

With the system prompt read during the load (`prefill`, added to both backends: 262 tokens are
then already cached when the life clock starts; checked on the laptop, the birth request then
reads 40 tokens), the birth thought is only the reading plus generation:

| Model | measured birth s | system prompt s | reading + 70 tokens s | full re-read of 810 tokens s |
|---|---|---|---|---|
| gemma-3-1b-it Q8_0 | 44 | 19 | 25 | 68 |
| gemma-3-4b-it Q4_K_M | 151 | 84 | 67 | 307 |
| llama-3.2-1b-instruct Q8_0 | 57 | 26 | 31 | 87 |
| llama-3.2-3b-instruct Q6_K | 176 | 103 | 73 | 351 |
| phi-4-mini-instruct Q4_K_M | 148 | 80 | 69 | 313 |
| qwen3-1.7b Q8_0 | 81 | 37 | 45 | 136 |
| qwen3-4b-instruct-2507 Q4_K_M | 165 | 93 | 73 | 343 |
| smollm3-3b Q6_K | 176 | 107 | 70 | 342 |

With prefill, **every candidate passes the 90 s birth test** (67-73 s for the 3-4B). But every
reload re-reads system + recall + reading in a fresh server: at recall 512 that is about 810
tokens, **5-6 minutes for a 3-4B** (S4's 180 s go fails before the load is even counted), 136 s
for Qwen3 1.7B, under 90 s for the 1B models.

**Leader for the rest of the spike: Qwen3 1.7B** (the only candidate that passes the birth test
without tricks and is clean in S6; its reload re-read fits S4's budget). Llama 3.2 3B, Qwen3 4B
and the other 3-4B stay candidates for the rehearsal, but on the Pi 4 they need the S4 fallback
(post-reload recall about 200 at reload 1 and 100 at reload 2, or no reload with precision
falling another way). Integrator's call at checkpoint A.



### S4: reload to the first token

S4_TEXT

### S2t: re-read timings on the Pi

S2T_TEXT

### S1c: 30 minutes of sustained generation

S1C_TEXT

### Using the numbers

`bench/measured/pi4-<model>-<step>-<threads>.json` are in the format `costmodel.load_costs` reads
(`step`, `threads`, `tg_tok_s`, `pp_tok_s`, `load_s`, `cache_reuse_works`). They are parked
outside `bench/` because `load_costs` applies every `bench/pi4-<model>-*.json` automatically,
and with measured Pi 4 speeds `pi4/compressed-2700` (and later the others) fails the
thought-count rule, which would break `make check` before the profiles are rebased (8.5: the
integrator updates the profiles, then re-runs `epitaph estimate`). To adopt them:
`git mv bench/measured/pi4-*.json bench/` together with the profile changes.
