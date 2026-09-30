# Contract changes

Agents propose changes to section 6 contracts here during a workflow round (BUILD_PLAN 0.3, 8.3).
The integrator decides between rounds, bumps `PROTOCOL_VERSION` in `types.py` for event changes,
and records the decision in CHANGELOG.md.

| # | Round | Agent | Proposal | Why | Decision |
|---|---|---|---|---|---|
| C-B1 | 0b r1 | B | Add `BannedHit` and `PushResult` to `types.py` (defined in `pacing.py` for now; the Pacer protocol in 6.4 names them) | The Pacer contract refers to them | |
| C-B2 | 0b r1 | B | Language packs: `config/lang/<language>.toml` (created, English), selected by `prompt.language`; loaded by `mind/prompt.load_lang`. Propose `config.py` validate that the pack exists and `docs/CONFIG.md` describe it. The pack also holds the 5.11 keyword, cliché and helpdesk lists for `verify.py` | Decision 19; B10 lists live with the strings they judge | |
| C-B3 | 0b r1 | B | New optional keys with code defaults (add to `default.toml`): `prompt.readings_memory_step = 0.05`, `prompt.readings_cores_step = 0.2`, `prompt.readings_speed_step = 0.2`, `reveal.min_rate_sample_s = 3.0`, `reveal.hesitation_inside_from = 0.1` | Reading "(was)" thresholds (Q1); the pacer's rate warm-up; inside-word hesitations "late in life" | |
| C-B4 | 0b r1 | B | `costmodel.py`: at a reload, cut memory to `recall × trim_to` (what `Memory.cut_for_reload` does) instead of `min(memory, recall)`; count the marker's tokens in the past | Match the real memory rule | |
| C-B5 | 0b r1 | B | Event notes: `word.char_ms` has one entry per character of `text` (spaces inside a word included); a hesitation before a word is applied by delaying the `word` event, so it is not a field. `forget` `upto_i` is inclusive | Displays (D) and verify-life (E) need the exact meaning | |
| C-B6 | 0b r1 | B | `backend/fake.py`: type the clock as a small protocol (`sleep`, `elapsed`) instead of `FakeClock`, so it runs on `clock.VirtualClock` (tests pass it with a type ignore) | Concurrent generation and typing need the virtual loop | |
| C-B7 | 0b r1 | B | Bench/pacer: the pacer's rate is **non-space letters per second**; the first-thought estimate should be `tg_tok_s × non-space letters per token` (about 3.4 for English BPE; `estimate.letters_per_token = 4.2` counts spaces). Propose `estimate.letters_per_token_nonspace = 3.4` or a measured value in `bench/` | Right first-thought cadence (5.12) | |

