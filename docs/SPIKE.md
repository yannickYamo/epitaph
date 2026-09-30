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

### S3: death by RAM — see below (rerun in progress)
