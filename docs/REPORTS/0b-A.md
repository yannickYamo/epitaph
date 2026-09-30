# Phase 0b, round 1: agent A (backend)

Branch `ws/a-backend`. Spike numbers and go/fallback calls are in `docs/SPIKE.md` (agent A's sections);
raw results in `bench/spike/`, Pi costs in `bench/measured/`, laptop costs in `bench/dev-*.json`.

## Headline for the integrator

1. **Prompt processing on the Pi 4 is 3-5x slower than the cost model's estimates.** 3-4B models
   read 2.3-2.7 tokens/s (estimate 10) and generate 1.0-1.4 (estimate 1.35). Their birth thought
   takes 148-176 s (go: 90 s) and a reload with recall 512 takes about 330 s (go: 180 s).
   Qwen3 1.7B passes both with small changes (birth 81 s; reload 135 s at recall 300 with 3 prompt
   threads). The 1B models are faster still but failed S6 hygiene (markup, Gemma 1B writes `[host]`
   readings itself).
2. **Cache reuse works** (warm trims and erosion re-read 2-8%), with two conditions:
   the memory-gap marker must not be inserted in front of kept turns (it re-reads about 80%: 6-8
   minutes on the Pi for a 3-4B), and Gemma 3 needs `--swa-full`. Proposals #3 and #6.
3. **Heat and power are fine** on the official supply: 30 minutes at 3 threads, no under-voltage,
   no throttling, 56 °C, 1.8 GHz throughout.
4. The measured Pi costs are parked in `bench/measured/`: applied, they make
   `pi4/compressed-2700` fail the thought-count rule, which would break `make check` before the
   profiles are rebased (QUESTIONS #5). `git mv bench/measured/pi4-*.json bench/` together with
   the profile changes.

## Built

- **A1** `tools/build_llamacpp.sh` (owned now): tag from `config/models.toml` (b11277), build log
  to `<prefix>/build.log` instead of hiding it, **deletes empty and pre-boot object files** (the
  brown-out left 4 zero-length `ggml-base` objects that made the Pi's `llama-server` link fail),
  `sync`, checks both binaries, and `--pi` mode that runs the build as the detached unit
  `llama-build` under the Pi lock. Both builds verified (below).
- **A8** `tools/download_models.py` (logic in `src/epitaph/backend/models.py`): `resolve` checks
  every repo and file on the Hub (refuses gated repos, lists what exists when a file is missing)
  and pins size, sha256 (the LFS oid) and revision in `config/models.lock.toml`; `fetch` checks
  free space (need + 2 GB margin), downloads with resume, verifies the sha256; `push` rsyncs over
  `pi-eth` into `/var/lib/epitaph/models/<model>/<quant>.gguf`, checks space on the Pi and the
  sha256 there. `models.toml` fix: bartowski has no Q2_K/Q3_K_M for Llama 3.2, so a per-quant
  `sources` override takes them from `unsloth/` (QUESTIONS #2). Downloaded (laptop, 32 GB)
  and pushed (Pi, 32 GB, 17 GB left): step 0 of all 8 candidates; full ladders + Q4_0 for
  Llama 3.2 3B, Qwen3 4B, Qwen3 1.7B, Llama 3.2 1B. Llama 1B Q4_K_M went to the Pi first (22:28).
- **A2 spikes** (scripts in `tools/spike/`, stdlib only so they run on the Pi's system Python):
  `llama.py` (server helper), `s6_templates.py`, `s2_cache_reuse.py` (S2f and S2t),
  `s1_fit_speed.py` (S1a/S1b), `s4_reload.py`, `s1c_soak.py`, `pi_run.sh` + `_pi_unit.sh` (a spike
  as a transient unit under the Pi lock, SSH drops retried, cable fallback, results rsynced
  back), `summarize.py` (the SPIKE tables).
- **A4** `backend/fake.py`, same API as phase 0a (the sim runs unchanged), plus:
  - a prompt cache that mirrors llama-server's `--cache-reuse` scan (the prompt only advances on
    a match, so removed text is skipped and inserted text stops reuse), switchable
    (`cache_reuse`, `cache_reuse_min`, default from `Costs.cache_reuse_works`); `prompt_n` in the
    final chunk is what was re-read; a (re)start empties the cache
  - `FakeFaults`: OOM, crash (signal or exit code), hang (alive, silent, no progress, released
    by kill/stop/resume), hang during load, crash on start, full context (`ContextFull`, and a
    truncated generation), slow load, slow prompt processing, slow generation; each at a token
    count or a clock time
  - `requests` log (prompt tokens, re-read, reused, first-token time), `prefill`, `status()` with
    pid, exit code, signal, speeds; `stop()` is not a death, `kill()`/`oom()`/`crash()` fire
    `on_death` (also between requests)
- **A5/A6** `backend/llama_server.py`: argv (`--jinja`, `-np 1`, `--cache-ram 0`, `--cache-reuse`,
  `--swa-full` for sliding-window models, `--load-mode none` when `mmap = false`, KV types,
  `-ngl 0`, `threads_batch`), spawn through `body.wrap_spawn` in its own session, `/health` with
  the load timeout, a watcher task firing `on_death` when the process exits on its own, stop
  with SIGTERM then SIGKILL, streaming chat and raw completion over SSE with the final timings,
  `CreatureDied` on a broken stream from a dead process, `ContextFull`/`BackendError` for server
  errors, `count_past_tokens` by render + tokenize (A7, exact per S6), `prefill`.
- `backend/errors.py`: `ContextFull`, `BackendError` (proposal #1).

## Tested

| Command | Result |
|---|---|
| `make check` (worktree) | ruff clean, pyright 0 errors, **108 passed** (3 deselected: model/pi), sim 2 lives, `estimate` PASS on all 5 Pi 4 profiles; total coverage 88% (backend: fake 99%, llama_server 91%, errors/base 100%, models.py 57%: the network/CLI paths) |
| Test merge with current `main` in a scratch clone (`git merge main`, then `make check`) | fake.py auto-merges with main's `_split_tokens` change; **538 passed**, estimate PASS. Conflicts only in `docs/CONTRACT_CHANGES.md` and `docs/QUESTIONS.md` (append-only tables: union) |
| `tools/laptop_lock.sh run A 15 -- env PYTHONPATH=src .venv/bin/python -m pytest -m model tests/templates` | **3 passed** against the real llama-server b11277 + Llama 3.2 1B: stream + timings + count; cache reuse after a front trim and an erosion step (at most 25%); SIGKILL mid-stream gives `CreatureDied` and `on_death` |
| `~/llama.cpp` laptop: `git describe --tags`, `llama-server --version`, `llama-bench` 3B Q6_K | b11277 / eae11d2; pp256 28.7, tg64 5.0 tokens/s (6 threads) |
| Pi: `git describe`, `llama-server --version`, `llama-bench` 1B Q4_K_M | b11277 / eae11d2, GNU 14.2 aarch64; pp64 9.9, tg32 4.5 (3 threads); `throttled=0x0` |
| `tools/download_models.py resolve --models all --quants step0,ladder` | every file found and pinned after the Llama `sources` fix |
| `tools/download_models.py push ...` | every file's sha256 matches on the Pi |
| Unit tests added | `tests/unit/test_backend_fake.py` (25), `test_llama_server.py` (14: MockTransport + a sleeping child process as the "server"), `test_models_files.py` (10) |

## Spike numbers (details and tables in docs/SPIKE.md)

| Spike | Numbers | Call |
|---|---|---|
| S6 | all 8 candidates: system role kept, exact counting (±1 token), DRY/grammar/prefill/raw OK; Gemma rejects two user turns in a row; hygiene: Gemma 1B echoes `[host]` + markup, Llama 1B markup | GO (per-model notes) |
| S2f | warm trim 2-7%, erosion 2-7%, without reuse 82-91%; first marker in front of kept turns 80-83% (in the reading: 4%); Gemma without `--swa-full` 100%, with 4-5%; `--cache-reuse 32` 4-12% everywhere | GO with marker change + `swa_full` |
| S1a | every step-0 fits at ctx 2048, f16 KV, mmap and `--load-mode none`: headroom 589-2,393 MB (Gemma 4B + swa-full 809) | GO |
| S1b | 3-4B: pp 2.3-2.7, tg 1.0-1.4, birth 148-176 s; Qwen3 1.7B Q8_0: pp 6.0, tg 1.65-1.84, birth 81 s; 1B: pp 9.4-12, tg 2.2-3.2; Q4_0 = Q4_K_M on the A72; 2 threads: pp -33%, tg -3% | 3-4B NO-GO without prefill (67-73 s with it); leader Qwen3 1.7B GO |
| S2t | Qwen3 1.7B: warm trim 19-21 s, erosion 16-19 s, late cut 37 s, reload re-read 93 s (587 tokens) | GO |
| S4 | Qwen3 1.7B: recall 512 → 181-250 s; recall 300, 3 prompt threads → 135 s; Q2_K recall 200 → 123 s; Llama 3B recall 512 → 330 s | Qwen3 1.7B GO with fallback (recall 300, `threads_batch` 3); 3-4B NO-GO at 512 |
| S1c | 30 min Qwen3 1.7B, 3 threads: no under-voltage, 0% throttled, 56.5 °C max, 1.8 GHz; tg 1.81 → 1.42 (21% drift, not thermal: likely KV growth) | heat/power GO; cost model should use late tg |

## Left

- **Cost-model adoption**: move `bench/measured/pi4-*.json` into `bench/` with the rebased
  profiles (integrator + B). The 3-4B models have measured costs at step 0 / 3 threads only.
- **S1c drift cause** is a hypothesis (KV cells with holes after reuse shifts); a restart-every-10-
  minutes soak or `/slots` against speed would settle it.
- **Leader choice is provisional** (speed + hygiene); the rehearsal decides the voice (A3, P0c).
- `tools/bench.py`, `docs/BENCH.md`, A3 rehearsal, A10 template golden tests: later phases.
- Llama 3.2 Q2_K/Q3_K_M come from unsloth (different imatrix than bartowski's); building them
  from bartowski's f16 + imatrix on the laptop remains possible if they read badly.
- Laptop mDNS: `epitaph.local` stopped resolving around 02:50; spike tools fall back to `pi-eth`.

## Contract proposals (docs/CONTRACT_CHANGES.md)

1. `ContextFull` and `BackendError` into `backend/base.py`.
2. `mmap = false` means `--load-mode none` (`--no-mmap` removed upstream); C's S3 must use `-lm none`.
3. The memory-gap marker goes into the reading after the first loss, not in front of the oldest
   kept turn (B's `mind/memory.py`); and the system prompt is rebuilt from kept groups, never cut
   by string replace.
4. (No change needed) thinking switch sent on every request instead of a `ModelSpec.thinking` flag.
5. `prefill(messages)` on the Backend Protocol (implemented in both backends).
6. `config/default.toml [backend]`: `cache_reuse = 32`, new `threads_batch = 3`.

## Questions (docs/QUESTIONS.md, each with the default in use)

- #2 Llama 3.2 Q2_K source (unsloth, pinned).
- #3 pins live in `config/models.lock.toml`.
- #4 the Pi has 2 GB of swap enabled (for C).
- #5 measured costs parked in `bench/measured/`.
- #6 leader for the Pi-only spikes: Qwen3 1.7B.
- #7 laptop mDNS failure; tools use the cable.
