# Phase 0c, round 2: agent E (QA)

Branch `ws/e-qa`, from `main` at `88a7916`. `make check` passes. Nothing was pushed.

Card: review 2, F2 (binding). Generation speed must never rise across a reload. This round
adds the `speed_monotonic` check to verify-life and to the cost model, with G0 and G2 rows.

## Headline for the integrator

1. **The check exists in both places and catches the round-1 case.** On a simulated
   `pi4/default` life, verify-life reports `reload at 28.2 min: 1.65 -> 2.41 tok/s (+46%)`,
   the owner's numbers. On the bench costs, the cost model reports 1.65 to 2.62 tokens/s at
   reload 1 (+59%) for both `pi4/default` and `pi4/compressed-2700`.
2. **ws/v-voice's rebased profiles (commit `7d822e4`) pass verify-life but not the cost
   model.** I merged them into a scratch worktree (nothing committed), where all 700 tests
   pass. Simulated lives pass `speed_monotonic` (0.97; reloads -3% and -8%). The cost model
   still reports a rise: +7% at reload 1 and +1% at reload 2 in `compressed-2700`. Voice
   matched the deep rates (`tg_tok_s`). My cost model gives each thought the speed for its
   own prompt size, and after a reload the prompt is short. Voice's PROFILES.md already
   flags this risk.
3. **On Qwen3 1.7B, no CPU share meets both F2 and the silence limit at reload 2.** With
   context-aware speeds, reload 1 passes at about 1.85 cores. Reload 2 needs about 1.35
   cores, and at that share its silence is 184 s (limit 180). Something else has to give.
   The options are a smaller post-reload recall, `threads_batch` not capped by the share
   (QUESTIONS A #9), or carrying the KV cache across the reload (F5).
4. **`make check` stays green while the profiles are pending.** `[estimate] speed_monotonic
   = "warn"` in `config/default.toml` turns a rise into a `WARNING` note. The gate command
   `estimate_measured.py --strict` (G0.4/G0.12) always treats a rise as a failure. At the
   merge, the integrator sets `"fail"` and removes the two strict `xfail` marks
   (CONTRACT_CHANGES E15). The marks go red as soon as both profiles pass, as a reminder.

## Built

**verify-life** (`src/epitaph/verify.py`)

- `Thought.tok_s` is read from `gen_end.tok_s`.
- `Verifier.thought_speeds()` gives each thought's speed. It uses `gen_end` rates when the
  life has any. Otherwise it uses `vitals.tok_s`, read as the previous thought's measured
  speed (what the reading reports, and what the rehearsal writes). So a thought takes its
  rate from the vitals written after it.
- `check_speed_monotonic` runs at the `full` and `rehearsal` levels. For each reload it
  averages up to `speed_monotonic_thoughts` (2) timed thoughts on each side, without
  crossing the neighbouring reloads. It fails when the ratio after/before is above
  1 + `speed_monotonic_tolerance` (0.05).
  - The value is the worst ratio. The detail lists every reload, for example
    `1.65 -> 2.41 tok/s (+46%)`.
  - The check is skipped when a life has no reloads.
  - A reload with no timed thought on one side (death during the silence) is listed but not
    judged.
  - The check fails when the reloads have no rate at all.
- The check is in the rehearsal summary (`SUMMARY_CHECKS`) and in the compare table
  (column "Speed after/before reload").
- Both thresholds are in `config/default.toml` `[verify]`.

**Cost model** (`src/epitaph/costmodel.py`)

- `Costs.tg_at(step, threads, share, context)` gives the speed at a given prompt size. It
  interpolates between the bench's short rate (`tg_tok_s_birth`, at the birth prompt size)
  and the deep rate (`tg_tok_s`, at `deep_prompt_tokens`). Outside that range it uses the
  rate at the nearer end. When the prompt sizes are unknown, it uses the deep rate.
  `load_costs` reads the prompt sizes from the bench files (qwen3-1.7b: about 282 and 834
  tokens).
- The estimate records each thought's speed at its own prompt size (system + memory +
  reading). Rule `speed_monotonic` is violated when the first thought after a reload is
  faster than the last one before it (no tolerance). Every report notes the speeds on both
  sides of each reload.
- The drift is left out on both sides, because whether a restart resets it is F8's open
  question. Thought timing is unchanged: it still uses the deep rate.
- `estimate.speed_monotonic = "fail" | "warn"`. Any other value raises.

**Gate tooling**

- `.github/scripts/estimate_measured.py --strict` forces `speed_monotonic = "fail"`.
- `docs/GATES.md`:
  - G0.12: the cost model on every profile and chosen model, plus the rehearsal compare
    column.
  - G2.6: `speed_monotonic` passes on each checkpoint B Pi life.
  - A coverage row for the new check.

**Tests**

- `tests/unit/test_verify_speed_monotonic.py` (23 tests), on crafted lives:
  - rise, slowdown, tolerance edges (+4%, +5%, +6%), tolerance from config, window
    averaging, windows bounded by the neighbouring reloads;
  - the vitals fallback with its one-thought lag, `gen_end` winning over vitals, unusable
    rates;
  - no reload, death in the silence, no rates at all;
  - which levels run the check, summary and compare output, the config defaults.
  - On simulated `pi4/default` and `pi4/compressed-2700` lives, an independent reading of
    their `gen_end` rates gives the same value and verdict, whatever the profile tuning.
- `tests/unit/test_costmodel_speed_monotonic.py` (12 tests plus 2 strict xfails):
  - a faster quant, slower at every reload, equal speed;
  - a rise caused only by the shorter context (and not seen without prompt sizes);
  - `tg_at` interpolation, clamping and share scaling; the prompt sizes read from `bench/`;
  - warn mode, an unknown mode, profiles without reloads, the G0 script in strict mode.
  - The two xfails: `pi4/default` and `pi4/compressed-2700` as tuned (the merge gate).
  - The crafted tests set every CPU share to the thread count, so profile tuning does not
    change them.
- `tests/helpers.slowing(events)` keeps a recorded life's speeds non-increasing. The
  rehearsal-tool tests use it, so they do not depend on how the profiles are tuned.

## Tested

| Command | Result |
|---|---|
| `make check` (this worktree, final) | green: ruff and pyright clean, 694 passed, 2 xfailed, coverage 95.8% (`verify.py` 99%, `costmodel.py` 98% on the focused run), sim 2 × 42 thoughts, `make estimate` PASS on all 5 Pi 4 profiles, with the `WARNING speed_monotonic` note on `default` and `compressed-2700` |
| Scratch merge of `ws/e-qa` + `ws/v-voice` (not committed), `pytest tests` | 700 passed, 2 xfailed |
| Same merge, simulated lives + `speed_monotonic` | `pi4/default` pass 0.973 (1.65 → 1.61, 1.61 → 1.48); `compressed-2700` pass 0.973 |
| Same merge, `estimate_measured.py --models qwen3-1.7b --strict` | `default` FAIL +7% at reload 1; `compressed-2700` FAIL +7% and +1% |
| Same merge, reload shares varied by hand (scratch only) | 1.85 / 1.40: reload 2 still +2%; 1.85 / 1.35: speed passes, reload 2 silence 184 s > 180 |
| `estimate_measured.py --bench bench/measured --profiles pi4/default pi4/compressed-2700 --strict` (this branch) | every 3-4B model and qwen3-1.7b fail `speed_monotonic` at reload 1 (+37% to +62%). Their step 1-2 rates are overlay estimates except for qwen3-1.7b, so only qwen3-1.7b's result means anything. The 1B models pass |
| `verify-life <sim life> --level full --json` | `speed_monotonic` row as in G2.6's command |

## Left

- **The integrator decides at the merge with ws/v-voice**: flip to `"fail"`, or keep
  `"warn"` until the profiles pass the context-aware model. Headline 3 is the trade-off.
  F8 (the S1c slowdown re-run with n_past logged and a restart variant) shows whether the
  short/deep gap is context or server age. Either way the first thoughts after a restart run
  near the short rate, unless the slowdown is server age that a restart does not reset.
- **The rehearsal charges the deep rate at every context** (CONTRACT_CHANGES E17), so
  rehearsal lives show less of the post-reload speed-up than the cost model predicts for
  the Pi.
- `sim.py` writes a forward-looking `vitals.tok_s` (E16). No check depends on it while
  `gen_end` carries rates.
- BUILD_PLAN 10.3 and 5.11 tables: add the check (E18, the integrator's file).
- G0.12 and G2.6 stay open until the profiles and the Pi lives exist.

## Contract proposals (docs/CONTRACT_CHANGES.md, "Proposals in phase 0c, round 2 (E)")

- E14: the `speed_monotonic` contract, the numbers the profiles must meet.
- E15: the flip to `"fail"` and the removal of the xfail marks at the merge.
- E16: `vitals.tok_s` means the last measured speed (the controller and sim follow the
  rehearsal).
- E17: the rehearsal clock charges context-aware generation rates.
- E18: add the check to BUILD_PLAN 10.3, 5.11 and 5.3.

## Questions (docs/QUESTIONS.md)

- E #12: how `make check` stays green until the profiles land (warn mode).
- E #13: context-aware speeds and the drift, with what they mean for the rebased profiles.
- E #14: window of 2 thoughts, 5% tolerance, edge cases.
