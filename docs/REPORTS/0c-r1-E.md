# Phase 0c, round 1: part E (QA and release)

Branch `ws/e-qa`, from `main` at `adbbd80`. `make check` passes. Nothing was pushed.

## Built

**Language-pack word lists (decisions C-B2 and E6)** in `src/epitaph/verify.py`

- `word_lists(cfg)` resolves the keyword, cliché, helpdesk and answering lists. For each list,
  the first source that has it wins:
  1. an explicit `[verify]` override in the config (for tuning experiments only)
  2. the language pack `config/lang/<prompt.language>.toml`, table `[metrics]`
  3. verify's built-in list, which is now only a fallback
- The pack's `persona` keyword list is the erosion list. `read_lang_metrics` reads the pack
  directly, so it also picks up an `answering` list once B adds one (E9).
- `verify.json` → `metrics.word_lists` names the source of every list. Today every list
  comes from `lang:en` except `answering`, which is `default`.

**Rehearsal readiness**

- **Header events and sidecars:** a header event (`type` `rehearsal`, `meta` or `header`,
  with or without `life`) and a `meta.json` or `rehearsal.json` sidecar are read as metadata,
  never as a life. The metadata fields are model, quant, persona, seed, stage, profile,
  hardware, `lifespan_s`, costs and run. `life_meta` merges them with `birth_loading` and
  `birth`; `birth` wins for the model, because it records what actually loaded.
- **Robust parsing:** lines without a `type` are ignored. Events without `life` form life 0.
- **Levels:** `default_level` checks rehearsal lives at the `rehearsal` level. Stage 1
  samples get the new `screen` level: text metrics only, without the sync rule, the recall
  budget or the thought-count rule.
- **Target lookup:** a folder whose only events file sits deeper is found. A folder with
  several events files names them and points to `--compare`.
- **`--summary`:** prints one JSON line (`summarize`) with life, source, model, quant,
  persona, seed, stage, costs, profile, hardware, level, ok, failed, thoughts, words, and the
  value and status of each 5.11 metric. The same record goes into `verify.json` as
  `summary`, next to `meta`.
- **`compare`:** `epitaph verify-life compare DIR...` (also `python -m epitaph.verify
  compare`, or `--compare`) verifies every life below the folders.
  - **Ranking:** by failed checks, then notice, reload noticing, demise, specific, clichés
    and 4-gram variety.
  - **Output:** a Markdown table where failing values are bold, then a per-model table and
    the gate G0 verdict. A model counts when every one of its full lives passes; screen
    samples are ranked but not counted.
  - **Options and exit codes:** `--out FILE` writes the table, `--json` prints the rows and
    `--require-models N` defaults to 2. Exit 0 when at least N models meet every threshold,
    1 when fewer do, and 2 when a life cannot be read. The other lives are still ranked
    when one fails to read.
- `reload_noticing.value` is now the rate (0 to 1), with "n of m" in `detail`.

**Gate G0 (`docs/GATES.md`)**

- Rows G0.1 to G0.11 carry the exact commands: measured costs per chosen model (`--strict`),
  bench coverage, rehearsal reports, the stage 1 ranking, "two models meet every threshold"
  with `compare --require-models 2`, the rehearsal's cost source, the lists and thresholds,
  and the checkpoint reply.
- G0.4 records this round's measured-costs result as evidence.
- The conventions now describe `verify-life` as wired, plus `--summary`, `compare`, and
  `voice/` being untracked. The 10.3 table shows which levels run each check.

**CI (`.github/`)**

- `.github/scripts/estimate_measured.py` runs the cost model for every (Pi 4 profile, model)
  pair on a bench folder. It lists which rates and load times a profile still takes from the
  overlay's estimates; `--strict` makes those a failure. G0.4 and CI both use it.
- New workflow steps:
  - "Rehearsal tools on simulated lives": the `--summary` line and the `compare` table on
    the simulated lives, with the output kept as evidence.
  - "Thought-count rule on measured Pi 4 costs, every model": informational (it passes the
    job even when the script fails), runs on `bench/` and `bench/measured`.
- The workflow header states that no step needs a model, llama.cpp or the Pi. Tests marked
  `pi` (tests/pi) and `model` (tests/templates) are deselected by pyproject's addopts;
  everything else under `tests/`, including the 26 new tests, runs in `make test`.

**Other**

- `tests/helpers.retext` takes `pause_after_ms`, so a longer rewritten thought still
  finishes typing before the next request.
- README mentions `verify-life compare`.

## Tested

| Command | Result |
|---|---|
| `make check` (worktree, final) | green: ruff clean, pyright 0 errors, **594 passed** (568 before; +26 in `tests/sim/test_verify_rehearsal.py`), `verify.py` coverage 99%, total 95%, `make sim` 2 × 45 thoughts (`oom`), `make estimate` PASS on all 5 Pi 4 profiles (estimated overlay costs) |
| `tests/sim/test_verify_rehearsal.py` | covers: pack lists and sources, config override beats pack, pack chosen by `prompt.language` (a `xx` pack in a temp config dir, `erosion`/`persona` alias, `answering`), missing pack falls back, pack without `[metrics]`, a pack-only cliché ("binary heart") and memory word ("tokens") judged, header not a life, header for another life ignored, typeless lines, lives without `life`, sidecars (precedence, non-object ignored), stage → level, screen check set, summary shape, `--summary` line and `verify.json`, one-target rule, nested single life, compare ranking with bold failures, per-model table, `--require-models 2` met and 3 not met (exit 1), JSON rows, a file with two lives (`#1`, `#2`), screen samples not counting for the gate, broken and misconfigured lives (exit 2, others still ranked), dedup of paths, rank order, pipe escaping, `epitaph verify-life compare` through `cli.main` |
| Existing verify tests | two texts changed because en.toml's lists are wider than the old defaults ("nothing" is a persona/demise word, "changed" a reload word) |
| CI dry run: every `run:` step of `ci.yml` (except apt and venv) under `bash -e` with the CI env, in a clean copy of the tracked and new files | all pass; 18 steps parse. Informational steps fail as expected: Pi 5 `skeleton-600` and `compressed-600` on both Pi 5 overlays, and the measured-costs step (below). Display: 91 passed |
| `epitaph verify-life compare <run>` on hand-made A-format folders (header event first, sim events) | ranked table and per-model verdict as intended; `--summary` prints one line with `costs: measured`, `persona`, `seed` from the header and `model` from `birth` |
| `.github/scripts/estimate_measured.py --bench bench/measured` | exit 1: 38 of 40 profile/model pairs pass (numbers below) |
| `... --bench bench/measured --models qwen3-1.7b --strict` | exit 0: every Pi 4 profile passes with every needed cost measured |

## Numbers

Thought-count rule on the measured Pi 4 costs in `bench/measured` (not yet used by
`make check`), per profile and model:

| Profile | Result | Thoughts (range over models) |
|---|---|---|
| pi4/default | all 8 models PASS | 41-51 (qwen3-1.7b 42) |
| pi4/smoke-300 | all PASS | 5 |
| pi4/skeleton-1200 | all PASS | 10-17 (qwen3-1.7b 15) |
| pi4/compressed-2700 | **FAIL** for llama-3.2-3b-instruct and qwen3-4b-instruct-2507: rule (a), 2 thoughts between the health changes at 6.5 and 11.2 min (need 3); the other 6 PASS | 33-41 (qwen3-1.7b 33) |
| pi4/unbounded | all PASS | 43 |

Only **qwen3-1.7b** has every rate and load time the profiles use measured. The other
models have step 0 at 3 threads only, so their `1-3`, `1-2` and `2-2` rates and their step 1
and 2 loads are still overlay estimates. The pass or fail above is therefore partly
estimated for every model except qwen3-1.7b.

## Left

- `answering` list in `en.toml` (B, E9). Until then `answering_readings` uses verify's
  default list.
- A's `epitaph rehearse` output is not on `main` yet. verify-life accepts the format
  proposed in E10 and anything close to it, but no real rehearsal life has been verified.
  G0.6 to G0.9 stay open.
- G0.4 and G0.5 need Pi bench files for the other steps of the chosen models (A, on the Pi),
  and L moving the files into `bench/`. `pi4/compressed-2700` needs retiming (B) if
  llama-3.2-3b or qwen3-4b stays a candidate.
- CI has not run the new steps on GitHub yet (no push). They were dry-run locally.
- Layout rows still wait on D's `verify_probe` (E8).

## Contract proposals (docs/CONTRACT_CHANGES.md)

- E9: add `[metrics] answering` to the language pack (B); verify already reads it.
- E10: the rehearsal output format (A): one folder per life plus a `rehearsal` header event
  (or `meta.json`) with model, persona, seed, stage, profile, hardware (the charged overlay,
  `pi4-4gb`) and costs.
- E11: `epitaph estimate --bench DIR` (L); also lint `.github/scripts` in `make lint`.
- E12: `make estimate` loops over the chosen models after checkpoint A (L).
- E13 (information, no L change): the verify-life CLI additions (`--summary`, `--compare`
  and the `compare` form, `--require-models`, `screen` level), `verify.json` `meta` and
  `summary`, and `reload_noticing` as a rate.

## Questions (docs/QUESTIONS.md, each with the default in use)

- Q8: a model meets the gate when every one of its full rehearsal lives passes; screen
  samples do not count.
- Q9: list precedence is config override, then the language pack, then the built-in list.
- Q10: "measured costs" for G0.4 means every rate and load time the profile uses
  (`--strict`); today only qwen3-1.7b qualifies.
- Q11: a rehearsal life is judged by the overlay it was charged at (header `hardware`); the
  gate commands pass `--hardware pi4-4gb`.
