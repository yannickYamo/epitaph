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
