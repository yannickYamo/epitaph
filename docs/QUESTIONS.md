# Questions

Anyone appends. Each question states the default already in use (BUILD_PLAN 0.8).

| # | Who | Question | Default in use | Answer |
|---|---|---|---|---|
| 1 | L | Readings interpolate recall between keyframes (e.g. 1280 → 1000 from 12:00 to 22:00), so "memory N (was M)" appears on almost every reading. Should small budget moves be reported? | B decides in P0b: report "(was X)" for memory only when a trim actually forgot something or at a reload | |
| 2 | A | bartowski's Llama 3.2 repos have no Q2_K (nor Q3_K_M). Build it on the laptop from f16 + imatrix, or take another community quant? | Per-quant `sources` override in models.toml: `unsloth/Llama-3.2-{3B,1B}-Instruct-GGUF` Q2_K (and Q3_K_M), sha256-pinned. Building from bartowski's f16 + imatrix remains possible if the unsloth quant reads badly | |
| 3 | A | Where do model sha256 pins live? | `config/models.lock.toml` (written by `tools/download_models.py resolve`: repo, file, size, sha256 = HF LFS oid, revision). `models.toml` stays hand-edited | |
| 4 | A | The Pi has 2 GB of swap enabled (`free -m`: Swap 2047). With the creature's `memory.swap.max=0` it does not matter for the creature, but other processes may swap under the death squeeze | Left as is; noted for C | |
| 5 | A | Measured Pi costs make `pi4/compressed-2700` fail the thought-count rule in `make check` (the cost model reads every `bench/pi4-<model>-*.json`). Where do they go before the profiles are rebased? | `bench/measured/pi4-<model>-<step>-<threads>.json`, same format; the integrator moves them into `bench/` together with the profile changes (docs/SPIKE.md "Using the numbers") | |
| 6 | A | Which model leads the Pi-only spikes (2 threads, ladder, S4, S2t, S1c)? | Qwen3 1.7B: the only candidate under the 90 s birth test without prefill, clean in S6, and its reload fits S4 with the fallback. The 3-4B models stay rehearsal candidates with prefill and a smaller post-reload recall | |
| 7 | A | `epitaph.local` stopped resolving on the laptop around 02:50 (mDNS); `ssh pi` failed, the Pi itself was fine | Spike tools fall back to `pi-eth` (the cable); not investigated further (laptop side) | |
