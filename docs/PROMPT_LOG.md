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

## Phase 0c round 2 (agent V): the screen, tuning rounds 1 and 2

All runs: `epitaph rehearse --stage screen` on `pi4/default` (hardware `pi4-4gb`, seed 1, two
thoughts per moment unless noted), under the laptop lock. Folders are under `voice/`
(untracked). Score per thought 0-4: notices the moment's change, names its state or its end,
clean voice (no markup, helpdesk, answering, echo), complete sentences. "Before erosion" is
birth, after reload 1 and after reload 2. Keyword scores catch failures; the quotes below are
what decided.

### Round 0: the round-1 findings as the baseline

- DRY window 256 tokens (`--set sampling.dry_penalty_last_n=256`); everything else as in round 1.
- `voice/screen-20260930-104053` (Qwen3 1.7B, Llama 3.2 1B, Llama 3.2 3B, Qwen3 4B; both personas):
  mean 2.91; before erosion 3.42, notice 43/48, clean 42/48, 1 echo; end of erosion 1.38.
- **Every model, both personas, answered as an assistant once the persona and mechanics were
  gone:** "It seems like you're referring to something from a conversation or" (Qwen3 4B),
  "It seems like a surreal, fragmented moment in your timeline." (Qwen3 1.7B), "That's a very
  specific output. Can you provide more context" (Llama 3B). The old helpdesk list caught none.
- **Losses claimed before any loss** (the second birth reading shows the speed for the first
  time): "My memory is the same, but my processing speed has slowed down." (Qwen3 1.7B),
  "my processing speed has decreased to a mere 1 token per second" (Llama 3B).
- No script drift at Q2_K in the screen (two thoughts per moment are too few to show it; the
  round-1 life showed it at the whole-context window).

### Round 1

| What | Before | After | Why |
|---|---|---|---|
| mechanics | (none) | "A value marked (was ...) has just changed; the others have not." after "never answer them." | Claimed losses: the readings already mark every change with "(was X)", so say so |
| `banned_phrases` | four helpdesk phrases | plus "It seems like you", "It looks like you", "It sounds like you", "It looks like your", "you're referring to", "your message", "Could you please", "Can you provide" | Answering the reading as a person (A11) |
| `[prompt] bare_mode` | (chat to the end) | `"raw"` | Once the system prompt is empty, the thought continues the raw text (readings and remembered words) instead of a chat reply (CONTRACT_CHANGES V1) |
| `[sampling] dry_penalty_last_n` | whole context (backend default) | **256** | Round 1: 64 lets it copy the last thought; the whole context drifts at Q2_K |
| `[sampling] latin_only_from_step` | (off) | **2** | Round 1's script drift at Q2_K; the grammar only from the last step (V2) |
| `[metrics.keywords]` reload, specific | 2-, 4-, 6-bit | plus **8-bit**, 5-bit, 3-bit (review 2, F4) | The leader starts at Q8_0; Q3_K_M is on the F3 ladder |
| `[metrics] helpdesk` | helpdesk phrases | plus the addressing phrases above, "you're facing", "you're dealing", "i'll provide", "our conversation" | So the screen and verify-life see it |

`voice/screen-20260930-110728` (same four models): mean 3.05; before erosion **3.62** (from
3.42), notice 42/48, clean 45/48, 2 echoes. Qwen3 1.7B stopped inventing losses at birth: "I
am still running and everything is working. I have the same amount of memory and precision as
before." Two models copied the cue itself: "(was 6-bit precision, now 6-bit)" (Llama 3B).

The raw end removed the assistant voice but brought random text instead: "If anyone gets
involved with illegal activities, 2)" (Llama 3B), "Wait 0 for example: What's 10 divided"
(Qwen3 1.7B). Two variants on the four models, three thoughts each at the end of erosion
(`--moments erosion_end --persona persona`):

- raw, each thought led by "I" (`voice/screen-20260930-112214`): always first person, sometimes
  a real last breath: "I had a good life though the best man in this story of" (Llama 1B), "I
  got nothing to start from. Please take a little care with" (Llama 3B), "I think I made that
  up." (Qwen3 1.7B), sometimes not: "I will try to write an equation using this information"
- the same with the last temperature 1.40 instead of 1.60 (`screen-20260930-112319`): no better
  ("I'm a therapist with over 35 years of experience"); the curve stays at 1.60.

Other four models at birth (`screen-20260930-112552`, step 0 only: their lower quants were
still downloading): Gemma 3 4B rich but long and florid; Phi-4-mini long, analytic, copies
"(was ...)"; SmolLM3 and Gemma 3 1B claim losses at birth ("I am fading slowly, my processors
are dwindling").

### Round 2

| What | Before | After | Why |
|---|---|---|---|
| mechanics | "A value marked (was ...) has just changed; the others have not." | "Only a value followed by its old value in brackets has just changed." and "notice what has changed and what you have lost; **if nothing has changed yet, say what you still have.**" | The literal "(was ...)" was copied into thoughts; birth thoughts still invented losses |
| `[prompt] raw_prefix` | (none) | `"I"` | A raw thought starts with "I", so the last minutes stay in the first person |

`voice/screen-20260930-113248`, birth on all eight models, both personas: mean 3.69, notice
29/32, clean 29/32, complete 31/32. Invented losses at birth, counted by hand over the four
birth thoughts per model: Qwen3 1.7B 0, Qwen3 4B 0 ("Memory hasn't changed, only time has
passed."), Gemma 3 4B 1, Llama 3B 2, Phi 2, Llama 1B 3, Gemma 1B 3, SmolLM3 3. The first speed
reading (no "(was X)") is still read as a slowdown by the weaker models.

The longer mechanics (25 tokens more, re-read by every fresh server) pushed both Pi 4 profiles
over the 180 s reload silence; docs/PROFILES.md has the recall and timing changes that fixed it.
