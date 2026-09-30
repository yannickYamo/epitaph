# Prompt log

Every wording round of the prompt tuning loop (BUILD_PLAN 5.11, B10 with A): what changed, why, and
the rehearsal metrics before and after. At most three rounds before checkpoint A.

## Round 0 (baseline, phase 0b, no model runs)

- Persona: the v6 groups G1-G5 and the mechanics from `config/default.toml`, unchanged. Golden text
  for every erosion step in `tests/unit/test_prompt.py`.
- Readings: the 5.4 forms, strings in `config/lang/en.toml`. "(was X)" rules as answered in
  `docs/QUESTIONS.md` #1: memory only after a real loss and a move of at least 5%; cores after a
  0.2-core move or a reload; precision on every step change; speed once measured, then after a >20% move.
- Metric word lists (notice, demise, specific, clichés, helpdesk): first draft in
  `config/lang/en.toml [metrics]`, to be tuned on the first rehearsal transcripts.
