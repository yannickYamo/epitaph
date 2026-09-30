# Spike results

Each spike records its numbers and a go or fallback decision (BUILD_PLAN 8.5). Raw results:
`bench/spike/*.json` (per run), `bench/measured/pi4-*.json` (Pi costs in the `costmodel.load_costs`
format, parked; see "Using the numbers"), `bench/dev-*.json` (laptop costs). Tables are printed by
`python3 tools/spike/summarize.py`.

## Agent A (backend): S6, S2f, S1a, S1b, S2t, S4, S1c

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

TABLE_S6

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

TABLE_S2

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

TABLE_S1

TABLE_BENCH

S1B_TEXT

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
