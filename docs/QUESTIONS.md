# Questions

Anyone appends. Each question states the default already in use (BUILD_PLAN 0.8).

| # | Who | Question | Default in use | Answer |
|---|---|---|---|---|
| 1 | L | Readings interpolate recall between keyframes (e.g. 1280 → 1000 from 12:00 to 22:00), so "memory N (was M)" appears on almost every reading. Should small budget moves be reported? | B decides in P0b: report "(was X)" for memory only when a trim actually forgot something or at a reload | |
| 2 | E | How is "typing speed at birth" judged: per thought or for the phase? | The **median** of the birth phase's thoughts (thoughts requested before the profile's second keyframe) must be in `wpm_birth_range`; **every** thought (3+ words) must be in `wpm_writing_range`. Speed = words ÷ (first letter of the first word → last letter of the last word), replaying `char_ms`, `pause_after_ms` and release times | |
| 3 | E | What counts as a CPU-share "change" for the notice rate, given that the share interpolates on every reading during erosion? | A drop of at least 0.25 cores since the last counted drop (`cpu_drop_min_cores`); the drop at a reload (threads 3 → 2) counts as part of the reload | |
| 4 | E | Distinct 4-gram ratio "per thought": each thought or the average? | Each thought with 8+ words before erosion must reach 0.5; the check reports the worst | |
| 5 | E | `pi5/compressed-600` fails `epitaph estimate` on both Pi 5 overlays (10 thoughts in 10 min). CI "estimate on every profile"? | CI runs the Pi 5 estimates as informational (warning, not red) until B retimes the profile; Pi 4 estimates are a hard gate; every Pi 5 profile must pass **simulation** (hard) | |
| 6 | E | The simulator's full lives fail `speed_decline` (last 5 min at ~45% of the first 5, limit < 40%) because the estimated step-2 rate (1.6 tok/s) is above step 0 (1.35) in `pi4-4gb.toml`. | Recorded as a finding (test_sim_findings); measured costs (S1b) decide. If real Q2_K is also faster than Q6_K, the CPU share must fall further or the threshold is re-tuned | |
| 7 | E | The simulator's birth speed on Pi 4 estimated costs is a median of 48 wpm (range 44-51): just inside the 45 wpm floor. | No change; flagged for B's cadence work and S1b: a slower model than 1.35 tok/s would fail `typing_speed_birth` | |
