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

## Phase 0c round 1: layout for the cache, no wording change (no model runs of the voice)

- **System prompt layout:** one paragraph per persona group, then the mechanics
  (`"\n\n".join(kept groups + [mechanics])`), rebuilt from the kept groups at each erosion step.
  Before, the groups were one paragraph. This is the layout every spike measured (S1b, S2f, S2t,
  S4), and an erosion step then removes whole paragraphs, so the server re-reads only the next
  reading (55 tokens, measured on the laptop with Qwen3 1.7B). Golden texts in
  `tests/unit/test_prompt.py`.
- **Memory-gap marker** (decision A3): `[host] earlier memory lost` is now the first line of the
  reading after a loss, in the same user message, and goes with that reading when it is
  forgotten; the next reading then carries it. Before, it stood in front of the oldest kept turn.
  The model still sees it from the first loss on.
- **Trims** cut whole turns while more than one turn is kept, so a thought is never shown to the
  model with its first words missing, except the last one left late in life.
- Wording of the persona, mechanics and readings: unchanged. The rehearsal (stage 1 and 2) judges
  the voice next.
