# Spike results

Each owner writes its own sections (BUILD_PLAN 8.2, 8.5). Numbers, then a go or fallback
decision. Raw results are committed next to the scripts.

## C: S3, S3b, S3c (part C, phase 0b round 1, 2026-09-29)

Setup: Raspberry Pi 4B 4 GB, kernel 6.18.50+rpt-rpi-v8, systemd 257, llama.cpp b11277
(A's native build), **Llama 3.2 3B Instruct Q4_K_M** (bartowski, sha256 `6c1a2b41…c728ff`),
the target model class. Official 5.1 V / 3 A supply: `vcgencmd get_throttled` was `0x0`
before and after every run below; CPU 41-56 °C.

Every spike runs `tools/spike/s3_probe.py` inside a throwaway `Delegate=yes` unit
(`epitaph-spike-<id>`, user `pi`) and drives the creature cgroup through the real body code
(`epitaph.body.cgroup.CgroupBody`), so the spikes prove the code path the controller uses.
Runner: `tools/pi_lock.sh run C <min> -- tools/spike/s3_run.sh <s3|s3b|s3c> [--model M]`.
Raw JSON: `tools/spike/s3_results/`.

### S3b: delegated cgroups and the network block — **GO**

`s3b-20260929-223554.json`. Every step passed as user `pi`, without root:

| Step | Result |
|---|---|
| Supervisor leaf | the unit's process moved into `supervisor/` |
| Controllers | `+memory +cpu +io` enabled in the unit's `subtree_control` (io works although the root cgroup does not list it before the unit starts: systemd enables it on the path for a delegated unit) |
| Creature leaf | created; `memory.oom.group=1`, `memory.swap.max=0` |
| Set and clear | `memory.high`, `memory.max`, `memory.swap.max`, `cpu.max`: each written, read back, cleared |
| Child via `wrap_spawn` | born in `…/epitaph-spike-s3b.service/creature`, affinity `[1, 2, 3]` |
| Counters | `cpu.stat usage_usec` 1.50 s; `io.stat rbytes` 68 MB (64 MB read after `FADV_DONTNEED`); `memory.stat` anon 67 MB, file 68 MB; `memory.events` present |
| OOM by `memory.max` | 60 MB anon child, `memory.max` 20 MB, swap off: killed in **0.033 s**, `oom_kill` 1, `oom_group_kill` 1; `death_cause` → `oom` |
| `cgroup.kill` | child dead in 0.016 s (SIGKILL); cgroup empty; `death_cause` → `manual` |
| Network before the rule | outbound TCP from the creature: connected |
| nftables rule | `socket cgroupv2 level 3 "system.slice/epitaph-spike-s3b.service/creature" oifname != "lo" reject` (root, added by the runner) |
| After the rule | creature → 1.1.1.1:80 **refused** (ECONNREFUSED); creature DNS **fails**; supervisor → 1.1.1.1:80 connected; creature ↔ 127.0.0.1 connected (the controller can still talk to llama-server) |

Notes for C8 (`body/netblock.py`) and C5 (units):

- nft resolves the path to a **cgroup id at load time** (`socket cgroupv2 level 3 10657`).
  If the creature cgroup is removed and recreated, the rule silently stops matching. So the
  creature cgroup is created once and kept (as 9 C8 says), and the rule is (re)loaded by a
  root `ExecStartPre=+` step after the body creates the leaf, or the body keeps the leaf
  across controller restarts (`setup()` reuses an existing `creature/`).
- The rule needs root; the controller does not. Fallback if nft is ever unusable:
  `IPAddressDeny=any` + `IPAddressAllow=localhost` on the controller unit (covers the whole
  subtree; the controller itself needs no outbound network).

**Recommended:** `creature_network = "blocked"` with the nft rule above. Go.

### S3c: CPU share with 2 threads — **GO**

`s3c-20260929-225256.json`. llama-server `-t 2`, pinned to CPUs 1-3, mmap, warm cache,
`cpu.max` period 100 ms, 48 generated tokens per level (the first token excluded from the
gaps), same prompt, `cache_prompt: false`.

| cpu.max | tok/s | vs 200% | ideal | gap p50 | p95 | max | first token | nr_throttled |
|---|---|---|---|---|---|---|---|---|
| 200% | 1.751 | 1.00 | 1.00 | 0.58 s | 0.61 s | 0.62 s | 24.3 s | 52 |
| 170% | 1.488 | 0.85 | 0.85 | 0.69 s | 0.71 s | 0.73 s | 28.6 s | 660 |
| 140% | 1.225 | 0.70 | 0.70 | 0.82 s | 0.87 s | 0.88 s | 34.7 s | 1398 |
| 110% | 0.965 | 0.55 | 0.55 | 1.07 s | 1.09 s | 1.10 s | 44.2 s | 2338 |
| 90% | 0.755 | 0.43 | 0.45 | 1.30 s | 1.59 s | 1.79 s | 56.1 s | 3534 |
| 70% | 0.579 | 0.33 | 0.35 | 1.69 s | 1.92 s | **2.79 s** | 88.8 s | 5257 |

- Speed falls in proportion to the share (within 6% of ideal at every level).
- Stalls: the worst token gap in the whole run was 2.79 s at 70%. Go criterion (no stall over
  20 s) passes with a wide margin. CFS throttling of the barrier-synchronised threads adds
  jitter below 1 core (p95/p50 1.14-1.22 at 90-70% against 1.02-1.06 above 1 core), which
  reads as hesitation, not as a stall.
- Prompt processing also scales with the share, and it is slow: about 2.3 tokens/s at 2
  threads for this prompt (the "first token" column is about 55 prompt tokens plus the
  first token). Passed to A for S1b/S2t: at 70% a 1000-token re-read would take about 25
  minutes, so late re-reads must stay small (cache reuse, 5.4) and the hang limit for the
  first token must use the share-scaled prompt speed (5.9).

**Recommended:** `cpu_share = true`; the profile's CPU-share column stands; no thread drop to 1
at reload 2. Period 100 ms (default) is fine.

### S3: death by RAM — **GO: `death_mode = "oom"`, load mode `dio`, limit below anon**

Runs: `s3-20260929-231811.json` (mmap, eviction probe), `s3-20260929-233619.json` (`none`,
`dio`), `s3-20260930-000233.json` (the controller's own `CgroupBody.squeeze_to_death()`).
`s3-20260929-223951.json` is **invalid** (kept for the record): the model's page cache was
charged to the rsync session, so an mmap creature showed 0 MB of file memory, and every
non-mmap trial failed at start (see "llama.cpp flag" below).

Method per trial: drop the model from the page cache (`FADV_DONTNEED`), start llama-server
(3 threads, ctx 2048) in the creature cgroup, generate 8 tokens, start a 400-token stream,
after 3 tokens write `memory.max`, poll until the process dies (30 s cap).

| Load mode | Limit | Kills | Time to kill | Creature memory before (anon + file) | Load time |
|---|---|---|---|---|---|
| `dio` (O_DIRECT into anon) | 0.5 × current | **5 / 5** | 0.33-0.35 s | 2231 + 19 MB | 51.3-51.9 s |
| `none` (read into anon) | 0.5 × current | **5 / 5** | 0.72-0.74 s | 2218 + 1269-1328 MB | 48.9 s |
| `mmap` | 0.5 × current (1118 MB, above anon) | **0 / 5** | no kill in 30 s: thrash, about 1.23 GB read from the card per 30 s (40 MB/s), 0-1 tokens | 299 + 1927 MB | 48.4-48.7 s |
| `mmap` | 0.5 × anon (149 MB) | 2 / 2 | 1.08-1.38 s | 299 + 1927 MB | 48.4 s |
| `mmap`, via `CgroupBody.squeeze_to_death()` | 0.5 × anon (149 MB) | **5 / 5** | 0.81-0.83 s | 299 + 1937 MB | 48.6-48.9 s |
| `dio`, via `CgroupBody.squeeze_to_death()` | 0.5 × anon (1115 MB) | **5 / 5** | 0.34-0.38 s | 2231 + 19 MB | 51.6-52.1 s |
Every kill: `oom_kill` and `oom_group_kill` rose, exit by SIGKILL, `death_cause` → `oom`.
`get_throttled` 0x0 throughout.

**Eviction probe (mmap, for the record; confirms 5.5 / V3):** `memory.high` just below the
working set (2237 MB):

| memory.high | tok/s | vs baseline 1.858 | card reads | token gap p50 / max |
|---|---|---|---|---|
| −1% (−22 MB) | 0.421 | 23% | 1.07 GB in 59 s | 2.6 s / 3.4 s |
| −5% (−112 MB) | 0.085 | 4.6% | 5.2 GB in 162 s | 12.3 s / 15.1 s |

Even a 1% eviction cuts the speed to a quarter: the weights are streamed once per token and
the evicted pages are re-read every token. No gradual RAM squeeze on this card.

**Findings that change other cards:**

1. **llama.cpp flag (A, backend argv):** b11277 has no `--no-mmap` ("error: invalid argument:
   --no-mmap"). It is `--load-mode none|mmap|dio|mlock|mmap+mlock` now. Contract proposal 3.
2. **Page-cache charging (A, C):** page-cache pages are charged to the cgroup that first read
   them. After an rsync, checksum or bench, an mmap creature's weights sit in *another*
   cgroup and no creature limit can reach them (the invalid first run). `dio` avoids the page
   cache entirely; for mmap, `drop_page_cache(model)` before the spawn.
3. **`none` doubles the memory:** the weights in anon plus the file in the page cache, 3.5 GB
   of the Pi's 3.7 GB for a 3B Q4_K_M. Reclaimable, but it evicts everything else. `dio`
   holds one copy.
4. **Death level:** `memory.max` must go below the *anonymous* memory; a level below
   `memory.current` is not enough for mmap. `CgroupBody.death_limit_bytes()` now uses
   `death_fraction` × anon (0.5).
5. **Warm reloads:** `dio` never benefits from a warm cache (always about 51 s for 2 GB);
   mmap loads in 4 s when warm. S4 (A) should time reloads with `dio`; the cost model's
   estimated 45-60 s load stands.

**Recommended:** `death_mode = "oom"`, `load_mode = "dio"` (mmap = false), `death_fraction =
0.5` of anon, applied at `end-0:30` (set in `config/hardware/pi4-4gb.toml`). The fallback
`death_mode = "deadline"` is not needed. S1a (A) must confirm that step 0 (Q6_K, about
2.6 GB anon) plus KV still leaves 300 MB free with `dio`.
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
   every candidate. Without it they re-read 82-91%. `trim_to = 0.85` stands; cache reuse stays on
   (256 works; 32 is better, see S2t).
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

**Addendum (phase 0c, rehearsal): word-level trims break reuse.** The S2f trims dropped whole
turns. `mind.memory.Memory` also trims inside the oldest kept turn (its reading, then its first
words: BUILD_PLAN 5.4 "order of loss"). The first rehearsal life showed trims re-reading
everything after the system prompt, and a probe with `Memory` on the laptop (Qwen3 1.7B Q4_K_M,
`--cache-reuse 32`, marker already present) confirmed it:

| Trim | Re-read |
|---|---|
| whole turns only | 70 of 460 tokens (15%) |
| ending inside a turn (`upto_i`) | 365-467 of 434-536 (84-87%) |

The cause is the same as the marker's: after a word-level cut, the prompt continues
`<end of the marker's message><assistant>` + the thought's remaining words, a junction that is
nowhere in the cache, and llama-server's reuse scan never moves past a new prompt position that
has no 32-token match. At the Pi's post-reload prompt rate (about 3.9 tokens/s at 2 cores) a
380-token re-read is about 100 s of silence, every few thoughts from reload 1 on. Proposal to B
in CONTRACT_CHANGES (A14): trim whole turns only, and cut inside a turn only when a single turn
is larger than the budget.

**Addendum: DRY's window.** At b11277 `dry_penalty_last_n` defaults to 64 tokens (seen in
`/completion`'s `generation_settings`), so DRY never sees the previous thought, and negative
values are rejected. The backend now sends the context size (QUESTIONS A #12).

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
| qwen3-1.7b | Q8_0 | 2 | mmap | 52.4 | 101.3 | 4.04 | 1.879 / 1.596 | 1182 | yes | 0x0 |
| qwen3-1.7b | Q8_0 | 3 | mmap | 52.2 | 81.2 | 5.96 | 1.84 / 1.653 | 1183 | yes | 0x0 |
| qwen3-1.7b | Q4_K_M | 2 | mmap | 32.7 | 90.7 | 4.08 | 2.646 / 2.143 | 2024 | yes | 0x0 |
| qwen3-1.7b | Q4_K_M | 3 | mmap | 32.3 | 68.0 | 5.89 | 2.837 / 2.412 | 2025 | yes | 0x0 |
| qwen3-1.7b | Q2_K | 2 | mmap | 24.0 | 100.1 | 3.64 | 2.512 / 2.036 | 2409 | yes | 0x0 |
| qwen3-4b-instruct-2507 | Q4_K_M | 3 | mmap | 60.5 | 165.2 | 2.36 | 1.261 / 1.047 | 774 | yes | 0x0 |
| smollm3-3b | Q6_K | 3 | mmap | 61.1 | 175.6 | 2.37 | 1.367 / 1.183 | 872 | yes | 0x0 |
| gemma-3-1b-it | Q8_0 | 3 | none | 27.2 |  | 12.43 |  /  | 2393 | yes | 0x0 |
| gemma-3-4b-it | Q4_K_M | 3 | none | 60.0 |  | 2.74 |  /  | 702 | yes | 0x0 |
| gemma-3-4b-it | Q4_K_M | 3 | mmap swa-full | 60.2 |  | 2.73 |  /  | 809 | yes | 0x0 |
| llama-3.2-1b-instruct | Q8_0 | 3 | none | 32.6 |  | 9.69 |  /  | 2108 | yes | 0x0 |
| llama-3.2-3b-instruct | Q6_K | 3 | none | 63.8 |  | 2.44 |  /  | 698 | yes | 0x0 |
| phi-4-mini-instruct | Q4_K_M | 3 | none | 60.0 |  | 2.72 |  /  | 801 | yes | 0x0 |
| qwen3-1.7b | Q8_0 | 3 | none | 51.5 |  | 6.33 |  /  | 1167 | yes | 0x0 |
| qwen3-4b-instruct-2507 | Q4_K_M | 3 | none | 60.0 |  | 2.42 |  /  | 773 | yes | 0x0 |
| smollm3-3b | Q6_K | 3 | none | 61.4 |  | 2.4 |  /  | 863 | yes | 0x0 |
| qwen3-1.7b | Q4_0 | 2 | mmap | 31.7 | 94.5 | 3.91 | 2.559 / 2.086 | 2075 | yes | 0x0 |

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
with `--swa-full`, which Gemma needs for cache reuse, 809 MB in the fit-only run). Anonymous memory with mmap is small (170-600 MB: KV, compute
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
- Threads (Qwen3 1.7B): 3 → 2 threads cuts prompt speed by about a third (5.9 → 4.0-4.1) but
  generation hardly moves (Q8_0 1.65 → 1.60 at depth). On the Pi 4 the thread drop at reload 1
  is not a visible slowdown; the CPU share (S3c) and precision do that work.
- The ladder (Qwen3 1.7B, 2 threads): Q8_0 → Q4_K_M → Q2_K generation 1.6 → 2.1 → 2.0 tokens/s,
  load 52 → 33 → 24 s. Lower precision is faster, not slower, until Q2_K: the loss the model
  is told about is real, but the display will not show it as slowness.
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

`tools/spike/s4_reload.py`: the step-0 server is stopped (SIGTERM), the page cache dropped
(cold) or the new file pre-read (warm), the new quant started at 2 threads, and the memory
re-read (system + recall + reading) up to the first token. "tb" = prompt threads (`-tb`).

| Case | cold | stop s | load s | re-read tokens | re-read s | total s | go (<=180 s) |
|---|---|---|---|---|---|---|---|
| llama-3.2-3b-instruct Q4_K_M t2/tb3 recall 512 | True | 0.5 | 49.8 | 898 | 279.3 | 329.7 | no |
| qwen3-1.7b Q4_K_M t2/tb3 recall 512 | True | 0.4 | 32.2 | 923 | 148.6 | 181.1 | no |
| qwen3-1.7b Q4_K_M t2/tb3 recall 300 | True | 0.4 | 32.4 | 647 | 102.2 | 134.9 | yes |
| qwen3-1.7b Q2_K t2/tb3 recall 200 | True | 0.4 | 23.5 | 555 | 98.8 | 122.7 | yes |
| qwen3-1.7b Q4_K_M t2/tb2 recall 512 | True | 0.4 | 32.6 | 923 | 216.6 | 249.6 | no |
| qwen3-1.7b Q4_K_M t2/tb2 recall 512 | False | 0.4 | 8.7 | 923 | 215.8 | 224.9 | no |
| qwen3-1.7b Q2_K t2/tb2 recall 200 | True | 0.4 | 23.9 | 555 | 145.7 | 169.9 | yes |
| qwen3-1.7b Q2_K t2/tb2 recall 200 | False | 0.4 | 8.1 | 555 | 145.5 | 154.0 | yes |

- **Qwen3 1.7B: GO with the fallback.** At the default post-reload recall 512 the reload takes
  225-250 s with 2 prompt threads and 181 s with 3 (fails the 180 s go by a hair). With 3 prompt
  threads and a post-reload recall of 300 it is 135 s, and at reload 2 (Q2_K, recall 200) 123 s.
  Load is 24-33 s cold, 8-9 s warm: the re-read dominates, so warm vs cold matters little.
- **Llama 3.2 3B: NO-GO at recall 512** (330 s even with 3 prompt threads; the re-read alone is
  279 s). A 3-4B on the Pi 4 needs a post-reload recall of about 100-150, or no reload.
- **Fallback applied in the recommendation:** `threads_batch = 3` (CONTRACT_CHANGES #6) and
  post-reload recall 300 at reload 1 for Qwen3 1.7B. The next reading can also be shortened; the
  system prompt (about 280 tokens) is the largest fixed part of every re-read.

### S2t: re-read timings on the Pi

Leader (Qwen3 1.7B Q8_0), 3 threads, `--cache-reuse 256`, marker appended to the reading
(`bench/spike/s2-pi4-qwen3-1.7b-marker-reading.json`). Prompt speed 5.0-6.4 tokens/s.

| Edit | tokens re-read | pause (prompt time) s |
|---|---|---|
| a normal turn | 96 | 15.5-18.5 |
| first trim with the marker (in the reading) | 103 | 20.8 |
| warm front trim | 96 | 18.7 |
| erosion step | 96 | 18.9 |
| cut to the late recall (about 170 tokens) | 230 | 37.2 |
| erosion at the late recall | 91 | 15.6 |
| re-read after a reload (587 tokens, fresh server) | 587 | 92.9 |

**GO:** warm trim pause 19-21 s (go: at most 30 s); erosion 16-19 s at 3 threads (go at late
settings: at most 90 s; at 2 threads and a 1.1-core CPU share the same 91 tokens take about 40 s).

Qwen3 re-reads 96 tokens per turn where the others re-read 40-52: its template drops the empty
think block from earlier assistant turns, so the cached thought no longer matches and the
~60-token piece is below the 256-token reuse chunk. With `--cache-reuse 32` (laptop,
`s2-dev-qwen3-1.7b-reuse32`): 54 tokens per turn and the late cut re-reads 12% instead of 51%;
Llama 3B and Gemma 4B are unchanged at 4-5%. **Proposed: `cache_reuse = 32`** (CONTRACT_CHANGES #6).

### S1c: 30 minutes of sustained generation

Qwen3 1.7B Q8_0, 3 threads, 30 minutes, thought after thought (70 tokens) with a rolling memory
of about 1,000 tokens; `vcgencmd` sampled every 5 s (`bench/spike/s1c-pi4-qwen3-1.7b.json`).

| Measure | Value |
|---|---|
| Thoughts | 32 |
| Under-voltage bit ever set | no (`get_throttled=0x0` at the end) |
| Samples throttled or capped | 0% |
| Max temperature | 56.5 °C (no heatsink or fan) |
| Lowest ARM clock | 1800 MHz |
| tg first 5 min / last 5 min | 1.807 / 1.424 tokens/s (drift 21%) |

**Heat and power: GO.** The official supply holds under sustained load: no under-voltage, no
throttling, the clock never left 1.8 GHz, 56 °C at most. Decision 22 (cooling) is not needed.

**Drift: 21%, over the 10% bound, but not thermal.** The speed falls steadily
(1.84 → 1.39 tokens/s) while the clock and temperature stay flat,
and it keeps falling after the memory stops growing (about minute 9). The likely cause (not yet
proven) is the KV cache filling up with holes: cache reuse shifts kept turns, and attention runs
over every used cell up to the highest one, so its cost grows toward the full 2,048 context. For the
cost model this means **using late-life generation speed** (about 1.4 tokens/s for Qwen3 1.7B Q8_0,
about 20% under the birth speed) rather than the birth speed. A check for next round: the same
soak with the server restarted every 10 minutes, or `/slots` n_past against speed.

### Using the numbers

`bench/measured/pi4-<model>-<step>-<threads>.json` are in the format `costmodel.load_costs` reads
(`step`, `threads`, `tg_tok_s`, `pp_tok_s`, `load_s`, `cache_reuse_works`). They are parked
outside `bench/` because `load_costs` applies every `bench/pi4-<model>-*.json` automatically,
and with measured Pi 4 speeds `pi4/compressed-2700` (and later the others) fails the
thought-count rule, which would break `make check` before the profiles are rebased (8.5: the
integrator updates the profiles, then re-runs `epitaph estimate`). To adopt them:
`git mv bench/measured/pi4-*.json bench/` together with the profile changes.
