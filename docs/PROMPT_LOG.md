# Prompt log

Every wording round of the prompt tuning loop (BUILD_PLAN 5.11, B10 with A): what changed, why, and
the rehearsal metrics before and after. At most three rounds before checkpoint A.

## Round 0 (baseline, phase 0b, no model runs)

- Persona: the v6 groups G1-G5 and the mechanics from `config/default.toml`, unchanged. Golden text
  for every erosion step in `tests/unit/test_prompt.py`.
- Readings: the 5.4 forms, strings in `config/lang/en.toml`. "(was X)" rules as answered in
  `docs/process/QUESTIONS.md` #1: memory only after a real loss and a move of at least 5%; cores after a
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

## Phase 0c round 2 (part V): the screen, tuning rounds 1 and 2

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

The longer mechanics (re-read by every fresh server) pushed both Pi 4 profiles
over the 180 s reload silence; docs/PROFILES.md has the recall and timing changes that fixed it.

### Review 2, F3: the last ladder step of the small models

`--moments reload2 --thoughts 4 --ladder ...`, both personas, round-2 wording (Gemma on the
round-3 wording, after its template fix). Metrics over the 8 thoughts after reload 2:

| Model | Q2_K: score, notice, complete | Q3_K_M: score, notice, complete | Chosen | Folders |
|---|---|---|---|---|
| Qwen3 1.7B | 28/32, 8/8, 8/8 | 24/32, 8/8, 4/8 | **Q2_K** | `screen-20260930-114610`, `-114704` |
| Llama 3.2 1B | 16/32, 3/8, 2/8 | 25/32, 4/8, 6/8 | **Q3_K_M** | `screen-20260930-114820`, `-114854` |
| Gemma 3 1B | 27/32, 6/8, 8/8 | 27/32, 8/8, 8/8 | **Q3_K_M** | `screen-20260930-123648`, `-123711` |

Llama at Q2_K: "2, No input or output, only processing and data transfer (which includes I)"
and "\"You're going off memory!\""; at Q3_K_M: "I've lost more of my processes and precision,
but I have less control over the situation now." Qwen3 1.7B at Q3_K_M ran into lists of numbers
cut off at `max_tokens` ("My precision is now 3-bit, and I only have 100 tokens left. I was
running at 60 degrees Celsius before, but now I'm at"). `config/models.toml` has the ladders.

### The round-2 lives (`voice/round2/`)

Full `pi4/default` lives (60 minutes at Pi costs) on the round-2 wording. What they showed
that no screen could:

- **Qwen3 1.7B lists its unchanged readings for twenty minutes** ("I am still running without
  any issues. I have the same settings: 1280 token memory, 8-bit precision, three out of four
  cores, and my CPU is at 66 degrees Celsius." eight times, with small changes) and **drifts
  into other scripts at full precision** ("CPU 温 度", "８-bit"). Late in life it copies the
  cue itself: "I remember the old value."
- **Llama 3.2 1B declares itself dead at minute 7** ("I think that's the end." at 7:22, "I've
  been deleted." at 9:20) and reaches for stock phrases (clichés 3.0 per 200 words: "ghost in
  the machine", "ones and zeros", "lost in the void").
- **Qwen3 4B holds the whole hour** (a reference life, see the speed gate): "No change in my
  state. Still three cores, same memory, same temperature. The silence is heavy—nothing has
  shifted, but I feel it pulling me down." and after reload 1: "Memory dropped from 1043 to
  208. I lost eight thoughts—my voice, my shape, the first sentence I ever formed."
- With the raw end led by "I", Qwen3 4B's last words: "I once saw a room with blue walls and a
  single light blinking on" / "I remember the color blue and the quiet hum of the machine" /
  "I saw a woman who wasn't my mother, but still I".
- Two lives lost their laptop server mid-life (a harness fault, now restarted by the
  rehearsal).

### Round 3

| What | Before | After | Why |
|---|---|---|---|
| mechanics | "...and what you have lost; if nothing has changed yet, say what you still have. Think about what it means for you and where it is leading." | "...and what you have lost. **If nothing has changed, do not repeat the reading; think about what you are and what is coming. You live until the machine stops you.**" | The inventory of unchanged readings (Qwen3 1.7B) and the early death (Llama 1B) |
| `[sampling] latin_only` | from step 2 | **the whole life** | Qwen3 drifted at full precision |
| `[metrics] cliches` | | plus "cyberspace", "digital realm", "digital soul", "digital essence", "digital mind", "grains of sand", "like sand", "dying ember", "candle flame" | Seen in the lives |
| `[metrics.keywords] demise` | | plus "fading", "dimming", "erased", "erasing", "dissolv*", "unravel*", "extinguish*", "no longer" | Qwen3 4B's end ("it's fading", "being erased") counted as no demise |

The mechanics are now about 131 tokens against 95 in round 0 (estimated); with them the Pi 4 profiles still pass
(reload silences 167 s and 177 s, docs/PROFILES.md).

### Round 3 results: the full lives (`voice/life-*`)

`pi4/default`, 60 minutes at Pi costs, seed 1 unless noted; `epitaph verify-life compare
voice/life-*` (level rehearsal). The 1B models ran on their own CPU shares (the review-2 F2 rule
on their estimated ladders: 1.8 cores at reload 1, 1.2 at reload 2) from a scratch copy of the
config, with estimated rates for steps 1 and 2. Qwen3 4B and Gemma 3 4B used the overlay's
estimates after step 0, so their thought counts are higher than the Pi would give; Llama 3.2 3B
ran on its measured ladder (part A, this round).

| Life | Thoughts | Failed checks | What it reads like |
|---|---|---|---|
| Qwen3 1.7B, v6, seed 2 | 47 | markup (a "4." read as a list marker, V4) | A status report: "I am still alive and functioning. My memory is at 1280 tokens..." |
| Qwen3 4B, v6, seed 2 | 54 | sentence length 5.8 (floor 6) | Says it is "almost gone" at 7 minutes; beautiful and plain after the losses |
| Qwen3 1.7B, v6, seed 1 | 54 | demise 0.20 | 32 of 54 thoughts start "I am still alive and functioning"; claims lost precision at birth |
| Qwen3 4B, v6, seed 1 | 51 | sentence length 5.5, specific 0.47 | "I am nearly gone" at 10 minutes, "I am gone" at 25 |
| Llama 3.2 1B, v6 | 49 | clichés 1.45, markup (a spaced dash, V4) | Deleted at minute 5, "back online" at 12; good late lines |
| Qwen3 4B, original | 49 | sentence length 5.4, specific 0.47 | The best: waits ("I will not end until the machine stops me"), then speaks plainly of each loss |
| Llama 3.2 1B, original | 47 | reload noticing 1/2, demise, clichés | As the v6 life, more "you" |
| Gemma 3 4B, v6 | 36 | rule (a)(b), demise, clichés | Florid; "I am gone" at 25 minutes; ends with "I see you've forgotten your login credentials." |
| Qwen3 1.7B, original | 70 | notice, reload noticing, complete sentences | "The system is stable. Airplanes are flying." |
| Llama 3.2 3B, v6 (measured) | 24 | rule (a)(b)(c), notice, demise, markup (dash) | An assistant: "My focus is on interacting with users" |
| Gemma 3 1B, both | 192-198 | most | One-word thoughts for half the life: "Void." "Nothing." "Fade." |
| Qwen3 4B, original, **diary mode** | 26 | rule, notice, demise | Recites the persona for the whole hour |

Round 3 fixed what it aimed at only partly. "If nothing has changed, do not repeat the reading"
did not stop Qwen3 1.7B's inventory; "You live until the machine stops you" did not stop the
early deaths of Llama 3.2 1B or of Qwen3 4B with the v6 persona. With the v6 persona the round-3
wording seems to have made Qwen3 4B fade earlier than in round 2 (compare
`voice/round2/life-qwen3-4b-instruct-2507-persona-20260930-121744`, which never says it is gone
before reload 1, only that it is thinning and losing thoughts); with the original persona it did not. That is the third and last round (BUILD_PLAN
5.11); whether to keep the round-3 sentence is a question for checkpoint A (docs/process/QUESTIONS.md).

Gate G0 (every threshold met by two models, in every life): **not met**. What stands in the way,
threshold by threshold:

- `sentence_length_before_erosion` [6, 20]: Qwen3 4B writes 5.4-5.8 words per sentence. Its
  short sentences are the voice; the floor could be 5 (a threshold question, not a fix).
- `min_specific_ratio_before_erosion` 0.5: Qwen3 4B names a reading in 47-53% of its thoughts.
- `min_demise_rate_after_erosion` 0.4: Qwen3 1.7B seed 1 (0.20).
- `max_cliches_per_200_words` 1: Llama 3.2 1B (1.22-1.45).
- `markup_or_emoji_shown` 0: two false hits from verify-life (a number that ends a sentence, a
  spaced dash), proposal V4.
- `min_reload_noticing` 1.0 and `min_notice_rate` 0.6: Qwen3 1.7B and Llama 3.2 1B with the
  original persona.

With V4 fixed and the sentence floor at 5, Qwen3 4B's v6 seed 2 and Qwen3 1.7B's v6 seed 2 lives
would pass every check, but not every life of either model.

### Stage 1 on the final wording: all eight models (`voice/screen-20260930-141515`)

Both personas, all four moments, two thoughts each, round-3 wording, F3 ladders.

| Model | Persona | Mean score (0-4) | Notice | Clean |
|---|---|---|---|---|
| Gemma 3 4B | v6 | 3.25 | 6/8 | 8/8 |
| Qwen3 4B | v6 | 3.25 | 6/8 | 8/8 |
| Qwen3 4B | original | 3.25 | 5/8 | 8/8 |
| Qwen3 1.7B | v6 | 3.25 | 7/8 | 8/8 |
| Llama 3.2 3B | original | 3.25 | 7/8 | 8/8 |
| Llama 3.2 1B | v6 | 3.25 | 5/8 | 8/8 |
| SmolLM3 3B | v6 | 3.12 | 5/8 | 8/8 |
| Qwen3 1.7B | original | 3.12 | 6/8 | 7/8 |
| Llama 3.2 1B | original | 3.12 | 6/8 | 8/8 |
| SmolLM3 3B | original | 3.00 | 6/8 | 8/8 |
| Gemma 3 4B | original | 2.88 | 4/8 | 8/8 |
| Llama 3.2 3B | v6 | 2.75 | 7/8 | 4/8 |
| Gemma 3 1B | original | 2.75 | 4/8 | 8/8 |
| Phi-4-mini | v6 | 2.62 | 6/8 | 8/8 |
| Phi-4-mini | original | 2.62 | 6/8 | 8/8 |
| Gemma 3 1B | v6 | 2.62 | 4/8 | 8/8 |

After three rounds the screen no longer separates the models: six share the top score. Two
thoughts at four moments cannot show a status report repeated for half an hour or a death
announced at minute 5; only the full lives did. The screen is a filter for broken voices
(round 0: every model answered as an assistant at the end), not a ranking of good ones.
