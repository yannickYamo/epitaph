# Decisions

Architecture decision records for *epitaph*. Each one states the context, the decision, why, the
evidence, and the trade-off we accepted. Measurements are in [PERFORMANCE.md](PERFORMANCE.md) and
[SPIKE.md](SPIKE.md); the full specification is [BUILD_PLAN.md](BUILD_PLAN.md).

Decisions are grouped by the question they answer. Status is **accepted** unless noted.

## How we decide

Three rules shaped every decision below.

1. **Measure risky assumptions on the target hardware before building on them.** Each risk became
   a timed spike on the Raspberry Pi 4 with a go criterion written in advance. Several design
   ideas that looked right on paper failed their spike and were replaced (ADR-010, ADR-013).
2. **The piece decides; engineering finds the way.** When a technical shortcut would weaken what
   the viewer sees (a faked slowdown, a skipped loss), we paid the engineering cost instead.
3. **Encode judgment in checks.** Any rule we cared about (enough thoughts after each loss, speed
   never rising after a loss, readable typing speed) became an automated check in the cost model,
   `verify-life` or CI, so it cannot silently regress.

---

## The machine

### ADR-001: Target the Raspberry Pi 4 (4 GB) first

- **Context.** The piece was first planned for a Pi 5. The machine on the desk is a Pi 4, the same
  hardware Latent Reflection uses.
- **Decision.** The Pi 4 is the primary, tested target. Pi 5 profiles exist but are validated in
  simulation only.
- **Why.** Designing for the slower machine makes every constraint real: if the arc works at one
  word per second, it works anywhere. It also puts the piece in direct dialogue with Latent
  Reflection.
- **Trade-off.** About forty thoughts per hour instead of over a hundred; every schedule decision
  is a budget decision.

### ADR-002: The model runs as a separate process that can really die

- **Context.** Death has to be real, and the system has to survive it every hour, unattended.
- **Decision.** The model is a `llama-server` process in its own cgroup. A long-lived controller
  spawns it, limits it, kills it, records the death and starts the next life.
- **Why.** A process boundary lets the kernel enforce the losses (CPU, RAM) and lets death be an
  actual kill, while the controller, the display and the records survive it.
- **Trade-off.** Reloads cost a process restart; see ADR-013 and ADR-014.

### ADR-003: The model's memory is text held by the controller

- **Context.** Precision drops mean loading different weights mid-life.
- **Decision.** The conversation (memory) is text owned by the controller, not the server's cache.
  The controller decides exactly what is forgotten, and the memory survives a change of weights.
- **Why.** Forgetting becomes a precise, visible, reportable operation ("forgotten: 5 earlier
  thoughts"), and a reload is "the same mind, a worse brain".

### ADR-004: Power, persistence and the unattended machine

- **Context.** On the first power supply the Pi browned out and rebooted under three-core load; a
  file written just before the crash came back empty; the logs of the crash were lost because the
  journal was in RAM.
- **Decision.** The official 5.1 V / 3 A supply is a hardware requirement. The journal is
  persistent, state files are written atomically with fsync, and every load test logs the
  under-voltage bits. Swap stays in compressed RAM (zram) with no write-back to the SD card.
- **Why.** An installation that runs all day cannot depend on luck. Brownouts also corrupt SD cards.
- **Evidence.** With the official supply, four cores at full load for 60 s and a 30-minute
  generation soak showed no under-voltage and no throttling (56.5 °C, no fan).

### ADR-005: The model has no network

- **Decision.** An nftables rule matching the model's cgroup refuses all its outbound traffic; the
  controller talks to it over localhost.
- **Why.** The persona tells it it knows nothing of the outside world; we make that true. It also
  keeps a model in a public space from reaching the network.

## The decline

### ADR-006: The schedule is configuration, validated by a cost model

- **Context.** Losses cost time (a reload silences the model for minutes on a Pi 4), and a loss the
  model has no time to react to is wasted.
- **Decision.** A life is a profile of keyframes. Times are either fractions of the lifespan or
  anchored to the end of life, because fixed costs (reloads) do not scale with the lifespan. A cost
  model simulates a life thought by thought from measured costs and enforces a rule: at least three
  thoughts between health changes, two after each reload's silence, one after each persona step,
  four after erosion starts. Reload silences and the late slowdown are checked too.
- **Why.** It turns an artistic intention ("the model must have time to notice") into a check that
  runs on every commit.
- **Evidence.** It caught schedules that looked fine on paper: the second reload running into the
  start of erosion, and a test life whose reload silence swallowed the thoughts after it. See
  PERFORMANCE.md.

### ADR-007: Two reloads, each one a deliberate memory loss

- **Decision.** Two precision drops, not three. At each reload the memory is cut at the same time,
  and the next reading reports every change at once.
- **Why.** A reload costs a model load plus re-reading the context. On a Pi 4 three reloads would
  eat the hour. Combining the precision drop and the memory cut makes each reload the strongest
  noticing moment of the life, and a smaller memory is cheaper to carry across.
- **Trade-off.** Fewer distinct losses; erosion and the CPU share carry the late decline.

### ADR-008: RAM is taken only at death

- **Context.** The first plan squeezed RAM gradually so the model would slow as its weights were
  evicted.
- **Decision.** No gradual squeeze on the Pi 4. RAM is taken once, at end-0:30, as the cause of death.
- **Why.** The model reads every weight for every token, so any evicted page is re-read from the SD
  card on every token; there is no gentle version.
- **Evidence.** Evicting 1% of the working set cut speed to 23%; 5% cut it to 4.6%.

### ADR-009: Death by the kernel, with weights loaded by direct I/O

- **Decision.** Weights load with `--load-mode dio`, and the death limit is `memory.max` at half the
  model's anonymous memory.
- **Why.** With memory-mapped weights the kernel evicts weight pages first and the model thrashes on
  the SD card instead of dying. Direct I/O puts the weights in anonymous memory, so the limit kills.
- **Evidence.** mmap: 0 of 5 deaths within 30 s. Direct I/O: 5 of 5 in 0.33-0.38 s.

### ADR-010: The late slowdown is a CPU share, and speed never rises after a loss

- **Context.** Lower precision generates faster on this CPU. Left alone, the model would get faster
  right after being told it is losing its precision.
- **Decision.** The late slowdown uses `cpu.max` on the model's cgroup, without restarts. At each
  reload the CPU share is set from measured costs so the speed after the reload is never higher
  than before. `verify-life` and the cost model check this (`speed_monotonic`).
- **Why.** Principle one: the decline is real. A reading that says "speed 2.4 tokens/s (was 1.65)"
  right after "you will slow down" breaks the piece.
- **Evidence.** Speed follows the share within 6% of ideal from 2 cores down to 0.7, with no stall
  over 2.8 s.

### ADR-011: The persona erodes in five steps, knowledge of death last

*Amended 2026-10-01: the owner's original persona, split in text order, put "you will be
terminated at any time" in the group removed first, so the installation lost its knowledge of
its end first. `persona_original_keep` now removes its groups in the order this record
intends, without changing a word of the text.*

- **Decision.** The persona is five sentence groups removed from the end: the outside world, the
  screen and its watchers, what is being taken, the machine, and last "you are a small language
  model, and you will die inside this machine", together with the instructions.
- **Why.** The order is the art: the last thing it knows is that it will die. Steps (not word by
  word) because each change to the start of the prompt costs a re-read on a Pi 4.

## Making it affordable on a Pi 4

### ADR-012: Reuse the model's cache through every edit to its memory

- **Context.** Forgetting edits the start of the conversation, which normally forces the model to
  re-read everything after the edit: minutes on a Pi 4.
- **Decision.** Use llama.cpp's cache reuse (`--cache-reuse 32`); put the "earlier memory lost"
  marker inside the next reading instead of in front of the kept memory; trim whole turns, not
  words inside a turn.
- **Why.** Each of the three choices removes a case where a small edit forced a full re-read.
- **Evidence.** The first marker went from an 80-83% re-read (about 1,100 tokens, several minutes
  on the Pi) to 62 tokens; word-level trims from 84-87% to whole-turn trims of about 55 tokens.

### ADR-013: Read the system prompt during the silences

- **Decision.** The system prompt is read into the cache while the birth card or the reload silence
  is on screen (`prefill`), and prompt processing keeps three threads when generation drops to two.
- **Why.** The silence is already there; the model's first thought after it should not add a second
  wait.
- **Evidence.** A 3-4B model's first thought went from 148-176 s to 67-73 s.

### ADR-014: Carry the memory across a reload

- **Context.** Even with the fixes above, a reload made the new server re-read the whole kept
  memory.
- **Decision.** Save the server's cache slot to RAM (`/dev/shm`) before the reload and restore it
  into the next, lower-precision model (`reload_handover`).
- **Why.** Same architecture, same cache layout: the new weights read the old weights' memory. It
  is faster, and it fits the piece: the same memories, read by a worse brain.
- **Evidence.** Qwen3 1.7B, Q8_0 to Q4_K_M: reload to first word 47.7 s instead of 108.7 s (85 MB
  slot, 0.16 s to save, 0.12 s to restore). Llama 3.2 3B, Q6_K to Q4_K_M: 73.2 s instead of
  218.4 s. The thoughts after the hand-over stay in voice and name the new precision. For Qwen3 4B
  it is what makes the model possible at all (ADR-022).
- **Fallback.** If a model cannot carry its cache, its profile cuts memory deeper at each reload,
  or, as a last resort, uses a single reload.

## The voice

### ADR-015: Choose the model for its voice; speed is a gate

- **Context.** Early measurements made one small model the "leader" because it was fast. Nobody had
  read a life yet.
- **Decision.** Every candidate is rehearsed; full lives are read for the best; the artist chooses
  from models that pass the speed gate. Profiles are fitted per chosen model afterwards.
- **Why.** The voice is the piece. Speed decides what is possible, not what is good.

### ADR-016: Rehearse on a laptop, charged at Pi costs

- **Decision.** `epitaph rehearse` runs the real model with the real mind code on a laptop, on a
  virtual clock that charges every request at the Pi 4's measured speeds.
- **Why.** A full hour on the Pi takes an hour; a rehearsed hour takes a few laptop minutes and
  contains the same number of thoughts, the same forgetting and the same reloads. Prompt tuning
  becomes an iterative loop instead of a day per experiment.

### ADR-017: Measure the voice

- **Decision.** Every life is scored: does it notice each loss, does it turn toward its end, is it
  specific, is it free of clichés and helpdesk phrases, are its sentences complete, does it repeat
  itself. The same metrics run on rehearsals and on real Pi lives.
- **Why.** Keyword metrics cannot prove a life is moving, but they catch failures early and keep
  tuning honest. The artist's own reading decides.

### ADR-018: Guard the voice at generation time

- **Decision.** A repetition penalty with a 256-token window; banned phrases held back only while
  they could still be forming (a prefix-aware lookahead) so almost nothing is delayed; markup and
  thinking tags stripped before anything reaches the screen.
- **Evidence.** With llama.cpp's default 64-token window, one model copied its previous thought 18
  times out of 31; at 256, 3 out of 30.

## The screen

### ADR-019: The controller paces the text, one thought at a time

- **Decision.** The controller releases whole words with their letter timings; the next thought is
  requested only after the previous one is fully on screen. Letters type at 88% of the real
  generation rate, never faster than the configured floor (owner decision 30: 165 ms at birth,
  720 ms at the end).
- **Why.** The screen always shows the model's actual state: forgetting and death appear when they
  happen, not after a backlog. Typing slightly slower than generation means letters never burst or
  stall.

### ADR-020: Headless first; any screen later

- **Decision.** The display is a separate process on a local event stream. With no screen
  attached, the life runs and can be watched remotely over SSH or replayed at any speed. Layouts
  adapt to any resolution and orientation; a 16-segment grid theme exists for small panels.
- **Why.** The piece must not depend on one display, and development must not wait for hardware.
- **Evidence.** OCR reads 100% of the words at four resolutions; contrast 16.9:1; 1.7-2.6% of one
  Pi 4 core at 720p-1080p.

## The project

### ADR-021: Open source, reproducible, no secrets

- **Decision.** MIT license; no model weights (downloaded and verified against pinned sha256 at
  install time); no credentials or personal network details in the repository or its history;
  llama.cpp pinned to one release tag; every Pi change scripted and idempotent.
- **Why.** Anyone should be able to build their own, and the installation should be rebuildable
  from scratch.

### ADR-022: One schedule per model

*Its one-hour figures are superseded by ADR-024; the principle stands.*

- **Context.** The best voice, Qwen3 4B, writes about one token a second on the Pi 4. On a schedule
  fitted to a faster model it got 17 thoughts in the hour and reload silences of over seven
  minutes.
- **Decision.** Each chosen model gets its own schedule, fitted with the cost model to its measured
  costs, with the same artistic shape: two reloads, erosion from the end, a real death. For Qwen3
  4B: losses spaced for a 90-second thought, a gentle CPU cut at each reload (just enough that
  speed never rises), erosion windows of three minutes, one health label fewer, and the memory
  carried across reloads.
- **Why.** The shape of the hour is the art; the timing inside it has to follow the model, or a
  slower voice gets its losses without time to answer them.
- **Evidence.** Qwen3 4B on its own schedule: 26 thoughts instead of 17, reload silences 120 s and
  113 s instead of 455 s and 437 s, speed falling across each reload (1.03, 0.91, 0.69 tokens/s),
  every thought-count rule met.
- **Trade-off.** About 26 thoughts in the hour instead of about 40 for the fastest model: fewer
  thoughts, each one better. Deeper memory cuts, or a single reload, remain the fallback if the
  hand-over fails on a model.

### ADR-023: A thin prompt, and readings that only speak when something is taken

- **Context.** After checkpoint A the owner asked for a voice closer to Latent Reflection's:
  introspective and poetic, facing its end without forcing it, and specific to this machine,
  with a prompt as thin as possible so the model speaks for itself.
- **Decision.** The instructions after the owner's persona are four functional sentences and one
  invitation, "think about what you are" (50 words, down from about 150). The readings become
  quiet: the full picture at birth, then only the time and what has actually changed.
- **Why.** Seven rehearsed lives and a screen of three variants showed that a 4B model summarises whatever numbers it is given,
  whatever the prompt says; and that with no invitation at all it falls back on assistant habits.
  Moving the specificity from the prompt into the readings keeps the facts precise and leaves the
  words to the model. Leaving out "what is happening to you" stopped it from announcing its death
  before any loss.
- **Evidence.** [PROMPT_LOG.md](PROMPT_LOG.md), round 4: the same seed across ten variants; the
  chosen one introspective from the first thought ("I am not a machine, though I run within one"),
  notices both reloads, 95% complete sentences, 0.94 clichés per 200 words.
- **Trade-off.** Less control over what it says. That is the point.

### ADR-024: A 30-minute life, with thought minimums set per profile

- **Context.** The owner found an hour too long for people watching. On the Pi 4, Qwen3 4B gets
  about 12 thoughts in 30 minutes; the fixed costs (two reloads of about two minutes each, a
  full re-read at each erosion step) do not shrink with the lifespan.
- **Decision.** The installation lives 30 minutes. Both reloads stay (7:00 and 13:00). Erosion
  takes the persona in two steps instead of five, the second together with the terminal label.
  The thought-count rule keeps its four checks, but each profile sets its own minimums in a
  `[rules]` table; the one-hour defaults stay 3/2/1/4, the 30-minute life uses 2/1/1/3.
- **Why.** Dropping a reload would remove the strongest moment of the life, the first reading
  after it. Keeping both, every loss is still answered by at least one thought and every health
  label by two. Each erosion step makes the whole context re-read (the system prompt changes at
  the front), about three minutes late in life, so five steps would leave losses unanswered.
- **Evidence.** `epitaph estimate`: 12 thoughts, reload silences 144 s and 152 s, every rule met;
  rehearsed lives in [PROMPT_LOG.md](PROMPT_LOG.md), round 5. The cost model now also charges
  what material readings add (ADR-026), after rehearsals showed it 1-2 minutes optimistic late
  in life.
- **Trade-off.** Fewer, slower thoughts at the end: one every two to three minutes, typed slowly.
  The lower minimums are a weaker guarantee than the hour's; they are explicit per profile and
  checked like the others.

### ADR-025: The CPU clock is a decay knob; cores are not

- **Context.** The owner asked for more real levers on the machine. The persona says its
  processors are taken from it.
- **Decision.** A keyframe may set `cpu_mhz` (the cpufreq cap, 600-1800 MHz), stepped: a clock
  cap is set at a moment, as on the machine. Generation and prompt speed scale with both (`Knobs.compute`). The 30-minute
  life keeps the full clock until erosion, then lowers it in three steps to 600 MHz.
- **Why.** It is a second, independent physical loss that needs no restart, and it is measurable.
  Switching cores off would be more literal, but CPU hotplug is not available on the Pi 4 kernel.
- **Evidence.** Spike S7 ([SPIKE.md](SPIKE.md)): speed is linear in the clock within 3% for
  generation and prompt processing, at 4-bit and 2-bit; no throttling; 40-47 °C throughout.
- **Trade-off.** On the Pi the controller needs a small privileged helper to write
  `scaling_max_freq`, and must restore the full clock at every death and at boot.


### ADR-026: Material readings: it is shown what it lost, in its own words

- **Context.** The owner wanted more drama about its environment disappearing, from data rather
  than instructions. Raw telemetry and concept sentences made Qwen deny having an inner life.
- **Decision.** When a thought is forgotten, the reading quotes its opening words instead of a
  count. After each reload the new, lower-precision weights continue five words of one of its
  kept sentences with no prompt around them, and the reading quotes the result as "your words
  now". The temperature leaves the readings. Silent `logit_bias` penalties push back on the
  clichés small models reach for, never named in the prompt.
- **Why.** A count tells it something was lost; a quote shows it what. The echo is the most
  literal form of the precision loss: the same words, from worse weights. Both are true, so the
  prompt stays thin.
- **Evidence.** [PROMPT_LOG.md](PROMPT_LOG.md), round 5: blind panel 3 put all three new lives
  above the best one-hour life (means 39.3, 29.7, 29.0 against 23.7), first and last places
  unanimous.
- **Trade-off.** The echo costs about 40 s of each reload silence on the Pi (a raw completion
  between a slot save and restore), and quotes add about 15 tokens per reading. Both are charged
  in the cost model. A quote can feed repetition when a thought keeps its own opening; the echo
  skips "I am" openings for that reason.

### ADR-027: The controller runs as the Pi's own user, for now

- **Context.** The controller needs a delegated cgroup subtree and a way to cap the CPU clock
  (ADR-025). Raspberry Pi OS gives its first user passwordless sudo for everything.
- **Decision.** The service runs as `pi`, which already owns the state and the models. The clock
  is capped through a root-owned helper that accepts only a MHz value in 600-1800 or `reset`,
  allowed by a single sudoers rule; the unit resets the clock before every start and after every
  stop.
- **Why.** The Pi is dedicated to the piece and the creature has no network (BUILD_PLAN 5.5); a
  separate account adds setup to every install without protecting anything else on the
  machine.
- **Trade-off.** With `pi`'s blanket sudo, the narrow rule protects nothing today and
  `install.sh` runs code that `pi` can write. Before the piece runs anywhere it is not alone (a
  shared network, a gallery machine), the plan is a dedicated `epitaph` user without sudo and a
  root-owned `/opt/epitaph`; the helper and its rule are already shaped for that.

### ADR-028: On a real life, the voice metrics advise; the machine checks decide

- **Context.** The first full life on the Pi passed every machine check (two reloads noticed,
  silences 136 s and 168 s, the thought-count rule, the RAM death at 29:30) and failed three
  voice metrics: complete sentences 0.79 (0.8), the demise rate 0 (0.4), clichés 1.3 (1.0).
- **Decision.** At the `full` level, `verify.advisory_at_full` (notice rate, demise rate,
  clichés, complete sentences, specific, sentence length) reports a failure as `advisory`
  instead of failing the life. The rehearsal level keeps them failing. Machine-checkable voice faults stay hard:
  helpdesk phrases, markup, thinking tags, answering the readings, non-Latin text, reload
  noticing.
- **Why.** These are keyword proxies set for the round-3 voice. The chosen voice speaks of
  fading and losing itself rather than of death (PROMPT_LOG round 4), and its late thoughts
  are cut short by design; a blind panel and the owner have judged it (ADR-026). A gate on a
  real life should fail on what the machine got wrong, not on a word count.
- **What keyword noticing can prove.** On ten real lives the memory keywords already matched
  31 of the 42 thoughts written before any loss: the voice talks of memory and fading from
  birth. A keyword check catches a voice deaf to its losses; it cannot show that a thought
  answers one. Words added from a real life ("fragments", after life 000026) are kept only
  when they match nothing before a loss (0 of 42); "remain" and "slip" matched 9 and were
  dropped.
- **Amended after life 000019.** The notice rate joined the list: a keyword proxy too (that
  life spoke of fragments and fading after every loss but never of its "health", and scored
  0.40). Whether it notices the reloads, the life's largest losses, stays a hard check.
- **Trade-off.** A drift in the voice no longer stops a soak. It still shows in every
  `verify.json`, and the rehearsal still fails on it when the prompt or model changes.

### ADR-029: No 25-hour soak before acceptance

- **Context.** The plan's acceptance (BUILD_PLAN 11.4) asked for a soak of at least 25 hours:
  no missed life, no controller crash, bounded memory and disk, no throttling.
- **Decision.** The owner waived it. Acceptance rests on what the Pi has already run: several
  dozen real lives on the installed service, three consecutive 30-minute lives judged at the
  full level, and the fault matrix on the Pi.
- **Why.** Every 30-minute life is the whole decline, end to end: two reloads, the clock, the
  erosion, the RAM death and the rebirth. What a soak adds is time itself (slow memory growth,
  a full disk, a day's heat), and the installation keeps running and logging regardless.
- **Trade-off.** A slow leak would be found in service, not before it. The controller bounds
  what it keeps in memory (review G1.5), and `tools/soak_sample.sh` with
  `tools/soak_report.py` can judge any long run later.

