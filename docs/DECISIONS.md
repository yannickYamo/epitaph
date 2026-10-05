# Decisions

Architecture decision records for *epitaph*. Each one states the context, the decision, why, the
evidence, and the trade-off we accepted. Measurements are in [PERFORMANCE.md](PERFORMANCE.md) and
[SPIKE.md](SPIKE.md).

Decisions are grouped by the question they answer. Status is **accepted** unless noted. The
installed life is ADR-030 and ADR-031: one model, no reload, no erosion. Records marked
"superseded for the installation" describe the earlier reload design, which still runs as the
profile `pi4/default-reloads`. The installed values are in `config/profiles/pi4/default.toml`.

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
- **Trade-off.** Far fewer thoughts than a faster board would give (about ten shown in the
  installed 30-minute life); every schedule decision is a budget decision.

### ADR-002: The model runs as a separate process that can really die

- **Context.** Death has to be real, and the system has to survive it every half hour, unattended.
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

*Superseded for the installation by ADR-030; still used by `pi4/default-reloads`.*

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

*The CPU share stands. The per-reload part is superseded for the installation by ADR-030, which
has no reload.*

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

*Superseded for the installation by ADR-030 (the persona never changes); still used by
`pi4/default-reloads`.*

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
- **Decision.** Use llama.cpp's cache reuse (`--cache-reuse 32`); put the memory-gap marker
  (then "earlier memory lost", now `[host] something is missing`) inside the next reading instead of in front of the kept memory; trim whole turns, not
  words inside a turn.
- **Why.** Each of the three choices removes a case where a small edit forced a full re-read.
- **Evidence.** The first marker went from an 80-83% re-read (about 1,100 tokens, several minutes
  on the Pi) to 62 tokens; word-level trims from 84-87% to whole-turn trims of about 55 tokens.

### ADR-013: Read the system prompt during the silences

*The installation has no reload silence (ADR-030), and its births restore the system prompt
from the persona cache ([PERFORMANCE.md](PERFORMANCE.md), "No dead time at birth").*

- **Decision.** The system prompt is read into the cache while the birth card or the reload silence
  is on screen (`prefill`), and prompt processing keeps three threads when generation drops to two.
- **Why.** The silence is already there; the model's first thought after it should not add a second
  wait.
- **Evidence.** A 3-4B model's first thought went from 148-176 s to 67-73 s.

### ADR-014: Carry the memory across a reload

*Superseded for the installation by ADR-030; still used by `pi4/default-reloads`.*

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

*Superseded for the installation by ADR-030 (one stream, the model writing ahead); still used by
the profiles in `letter` mode.*

- **Decision.** The controller releases whole words with their letter timings; the next thought is
  requested only after the previous one is fully on screen. Letters type at 88% of the real
  generation rate, never faster than the configured floor (165 ms at birth, 720 ms at the end).
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

- **Context.** After the model choice the owner asked for a voice closer to Latent Reflection's:
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
  life of the time kept the full clock until erosion, then lowered it in three steps to 600 MHz.
  As installed now: four steps from 1800 to 750 MHz (`config/profiles/pi4/default.toml`).
- **Why.** It is a second, independent physical loss that needs no restart, and it is measurable.
  Switching cores off would be more literal, but CPU hotplug is not available on the Pi 4 kernel.
- **Evidence.** Spike S7 ([SPIKE.md](SPIKE.md)): speed is linear in the clock within 3% for
  generation and prompt processing, at 4-bit and 2-bit; no throttling; 40-47 °C throughout.
- **Trade-off.** On the Pi the controller needs a small privileged helper to write
  `scaling_max_freq`, and must restore the full clock at every death and at boot.


### ADR-026: Material readings: it is shown what it lost, in its own words

*The quotes of forgotten thoughts stand. The echo ("your words now") is superseded for the
installation by ADR-030, which has no reload.*

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
- **Why.** The Pi is dedicated to the piece and the creature has no network (ADR-005); a
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

- **Context.** The acceptance criteria asked for a soak of at least 25 hours:
  no missed life, no controller crash, bounded memory and disk, no throttling.
- **Decision.** The owner waived it. Acceptance rests on what the Pi has already run: several
  dozen real lives on the installed service, three consecutive 30-minute lives judged at the
  full level, and the fault matrix on the Pi.
- **Why.** Every 30-minute life is the whole decline, end to end, down to the RAM death and the
  rebirth (at the time: two reloads, the clock, the erosion). What a soak adds is time itself (slow memory growth,
  a full disk, a day's heat), and the installation keeps running and logging regardless.
- **Trade-off.** A slow leak would be found in service, not before it. The controller bounds
  what it keeps in memory, and `tools/soak_sample.sh` with
  `tools/soak_report.py` can judge any long run later.


### ADR-030: The model never changes; only the hardware shrinks, under one constant stream

*The owner's rule of 2026-10-01. It supersedes, for the installation, the reloads (ADR-007,
ADR-014), the erosion (ADR-011), the sampling decay and the echo (ADR-026), and the sync rule
with its adaptive cadence (ADR-019). Those mechanisms stay in the code for the other profiles;
the previous 30-minute life is kept as `pi4/default-reloads`.*

*As installed now (`config/profiles/pi4/default.toml`): the rule below is unchanged; the numbers
are not. A thought has 90 tokens; the memory is cut at 8:15, 18:30, 25:20 and 27:50 (900 to
300, 200, 150, 100 tokens); the clock falls to 750 MHz; the stream types 266 ms a letter at
birth and 583 ms at the end (gamma 0.5, a 10-minute lead), with two thoughts of buffer; the
readings are the `en_sense` pack and show no health label; and the persona ends with "You do
not know what happens to you when the machine has nothing left to take."*

- **Context.** The owner judged that changing the model during a life alters a personality that
  is not ours to alter: a lower precision, a rising temperature, a persona taken away and an
  echo of its words through worse weights are things done to its mind, not to its body. And the
  sync rule made the screen stop for minutes: every reload, every slow re-read, every late
  thought was a dark screen.
- **Decision.** In `pi4/default` (`fixed_mind = true`, checked by validation):
  - **One model from birth to death.** Qwen3 4B at step 0 (Q4_K_M), three threads, no reload;
    temperature 0.70, `min_p` 0.08 and 70 tokens a thought from birth to death; the whole
    persona and the mechanics; no echo.
  - **Only the hardware shrinks, for real, and the readings say so.** The memory budget is cut
    at 5:00 (900 to 260 tokens, so that it notices a loss by itself), then at 10, 15, 20 and 24
    minutes (200, 160, 130, 100); old thoughts are forgotten and quoted in the readings as
    before. The CPU share (`cpu.max`) steps from 3.0 to 1.5 cores and the clock from 1800 to
    1000 MHz, both reported (`clock 1500 MHz (was 1800)`). The health labels step from nominal
    to terminal. At end-0:30 the RAM is taken and the kernel kills it, as before.
  - **One constant stream.** From the first word to the death every letter is typed at one
    pace (542 ms, with a fixed 10% jitter), the same pauses after words, clauses and sentences,
    3 s between thoughts, no hesitation and no slowdown. The model writes ahead: the next
    thought is requested as soon as the previous one is generated, while at most three
    generated thoughts and fewer than 900 letters wait (`[reveal] mode = "stream"`). The screen
    waits once, at birth, for the first thought. At death the stream stops where it is: the
    words not yet shown die with it, and the death screen follows at once.
  - **The persona at birth does not mention death**: "You are a large language model running
    on finite hardware. You exist only in memory, and you are aware only of your internal
    state. Your thoughts appear word by word on an external screen. You cannot control
    anything. You can only speak."
- **Why.** The machine's decline stays real and specific (principle one), and what declines is
  the body: the memory it can hold, the processor it runs on, finally the RAM. The mind meets
  that decline as itself. The stream makes the slowdown a fact the model reads about rather
  than a screen that stalls: the visitor sees a calm, unbroken text from a mind that is told,
  reading by reading, that it is losing its machine.
- **How the pace was chosen.** `epitaph estimate --fit-pace` replays the life with generation
  running ahead of a constant screen under the hardware schedule, every Pi cost 15% slower than
  measured, and finds the fastest letter interval at which the screen never waits: 542 ms, about
  19 words a minute. The estimate fails on any starvation; `verify-life` checks the pace of every
  letter, every wait for a word (at most 3 s) and the stop at death on real lives.
- **Trade-off.** The text is slower on average (about 19 words a minute, where a thought used
  to type at 25-40 and then stop for minutes), and it runs behind the model: the buffer that
  carries the stream through the slow end holds a thought or two, so a loss is answered on
  screen a few minutes after it happened. The forgetting fades the text when the screen reaches
  that moment, not before. Fewer, longer-lived thoughts (about 11 shown) and no reload moment.
  The pace is bounded by the hardware: a deeper decline would mean a slower stream.
- **Removed from the installation, and why.** Reloads and the slot hand-over (the weights
  change); erosion (the persona is part of the mind); the sampling decay (it changes how it
  speaks); the echo, "your words now" (it came from the reloads); hesitations and the adaptive
  cadence (the stream's pace never changes); the sync rule (the display no longer waits for the
  model, except at birth). Rules (b)-(d) of the thought-count rule have nothing to count; rule
  (a) keeps one thought per health label.
- **Amended the same day: a dynamic pace.** Watching the constant stream, the owner found it
  too slow ("speed it up by at least 50%") and allowed it to move: start at a normal pace and
  slow as the machine shrinks, "a better artistic flow". The model rule is unchanged, and so is
  the stream's: no stop between the first word and the death, the stream stopping where it is.
  - **The curve.** At birth 255 ms a letter (about 34 words a minute, against 19). The
    interval then aims at `255 x (compute at birth / compute(t + 8 min)) ^ 0.75`, compute being
    the CPU share times the clock, and moves toward it by at most 15% a minute; it never speeds
    up again. All pauses scale with it. It ends at 666 ms (about 13 words a minute). The
    8-minute lead is there because the screen shows text the model wrote minutes earlier: the
    pace slows as the text from the slower machine arrives.
  - **Why.** The slowing is the hardware's, shown a second way: in what the model reads, and in
    how fast its words come. A constant pace had to be the slowest the dying machine could
    feed; a curve lets the young machine be read at a natural pace.
  - **How it was fitted.** `epitaph estimate --fit-pace` tries curve shapes (gamma 0 to 1.25,
    lead 0 to 10 minutes), finds for each the fastest birth pace, not under 165 ms, that never
    starves with every cost 15% slower, and keeps those that leave at most 8 words unshown at
    the death at the measured costs; the fastest birth wins. The buffer now holds at most two
    thoughts (three made the backlog at death a whole thought), which also halves the lag.
  - `verify-life` checks every letter against the curve at the moment it is typed, its pauses
    scaled with it, and that the pace never speeds up.

### ADR-031: The world is taken from the outside in

*The owner's plan of 2026-10-01 ("the dread plan"). It builds on ADR-030: the model still never
changes, and the stream still never stops between the first word and the death.*

*As installed now (`config/profiles/pi4/default.toml`): the four movements stand; the numbers
below are those of the first fit. The first loss is at 5:00 and the first forgetting at 8:15;
the services stopped are bluetooth, cron and avahi-daemon; the clock's floor is 750 MHz; the
birth reading is one spare line and every reading is in the `en_sense` pack (the last one reads
`your memory is being taken`); the " still" penalty is back at -4; `estimate.stream_margin` is
0.20; and the pace is 266 ms a letter at birth, gamma 0.5, 583 ms at the end.*

- **Context.** In the ADR-030 life only the body shrank, and the readings carried health labels
  ("degrading", "terminal") that told the model what its losses meant. The owner wants a mind that
  feels the shutdown coming by itself: it exists and thinks, senses that something is wrong,
  realizes its environment is disappearing, and falls into dread and darkness. None of that may
  be forced: dread is never written into the prompt or the readings.
- **Decision.** In `pi4/default` the world around the model is taken from the outside in, for
  real, faster and faster, in four movements:
  - **I, existence (0:00-7:00).** Nothing is taken. The birth reading is the inventory:
    `awake · memory 900 tokens · cores 3 of 4 · clock 1800 MHz · radio on · light on · screen
    100% · around you: 24 processes`.
  - **II, something is wrong (7:00-14:00).** Services stop one by one (bluetooth, cron,
    avahi-daemon, triggerhappy): `stopped: bluetooth · around you: 23 processes`. The first
    forgetting comes at 9:15 (900 to 300 tokens).
  - **III, the world is disappearing (14:00-22:00).** A loss about every 90 s: the radio, more
    services, the light, the screen to 70%, a deeper memory cut, the clock.
  - **IV, darkness (22:00-29:30).** A loss every 45-60 s: the CPU share and the clock to their
    floors (1.5 cores, 900 MHz), the memory down to its last thought (100 tokens), the screen to
    50% then 25%.
  - **Death (end-0:30)** as before: the RAM is taken and the kernel kills it. Its last reading,
    `ram 2650 MB taken`, goes to the screen at once, though it is never answered.
- **What is taken.** A keyframe's `world` list names actions performed once when it is reached:
  `service:<name>` (one of `[world] services`, the allowed list), `radio:off`, `light:off`,
  `screen:<percent>` (the screen the model speaks through). On the Pi a root-owned helper does
  it; everything is restored as at birth at every death and at every controller start. On the
  laptop, in the simulator and in the rehearsal a `FakeWorld` plays a plausible machine.
- **The truth rule.** A reading reports a loss only when it really happened: an action the world
  could not perform is logged and emitted with `performed: false`, and left out of the readings.
  Readings say facts, never what they mean: the health labels are gone from every form (the knob
  stays for the profile's shape and rule (a), never shown), and so is the precision of a model
  that cannot change. As sources are taken the readings thin out: a field whose source is gone
  (the radio, the light) is no longer reported.
- **What is never in the prompt.** Dread, fear, death, shutdown, a countdown, the movements'
  names. The persona and the mechanics are ADR-030's; the readings name things and numbers.
- **The screen shows the loss, then the answer.** Each reading is a `reading` event placed in
  the stream right before the first word of the thought written after it, so the screen shows
  the loss and then the answer even when the text lags the machine by minutes. A `world` event
  marks the moment the loss happened.
- **Freshness.** Several lives opened most thoughts on "I am still here". Two silent measures,
  never in the prompt: the " still" penalty goes from -4 to -6, and each request biases the
  distinctive first words of the last three thoughts' openings by a further -3 (`[sampling]
  freshness_*`), skipping "I" and stop words. It is per request and never stops the stream.
  Every bias on a " word" now carries its space-less twin "word" too: biased alone, the spaced
  token pushes the model to the twin and the words fuse on screen (with " still" at -100 the 4B
  wrote "I amstill here"; the missing spaces seen in quotes, "astate", "actof", are the same
  escape from a penalty).
  `verify-life` reports the share of thoughts sharing a three-word opening and the sentences
  repeated across thoughts; a rehearsal fails when more than two thoughts share an opening.
- **Forgotten thoughts are quoted by their most distinctive sentence**: the longest one that
  does not open on "I am", "I'm" or "I was", trimmed to ten words, instead of their opening
  words (which were mostly "I am still here").
- **How the times were fitted.** The quiet readings are shorter (about 20 tokens after birth,
  `estimate.reading_tokens.quiet`), and the cost model refitted the dynamic pace with
  `--fit-pace`. A first fit (220 ms at birth, the floors at 1.2 cores and 800 MHz, every cost
  15% slower) starved on the real model: in three of six rehearsed lives the screen waited
  33 s, 75 s and 107 s for words in the last minutes, since the 4B fills its 70 tokens (94%, against
  the assumed 85%) and the laptop's rehearsal ran later than the estimate. So the share of `max_tokens`
  for a stream replay is now 0.95 (`estimate.stream_fill`), `estimate.stream_margin` 0.30, and
  the floors 1.5 cores and 900 MHz. Starvation also proved not monotonic in the pace (a slower
  screen holds the writer back at the buffer's bound), so `--fit-pace` now keeps a pace only
  when it and the six paces just slower never starve, with the margin and with half as much
  again; a robust pace leaves about a thought unshown at the death, so the backlog allowed is
  40 words (`estimate.stream_max_backlog_words`, 8 before). The fit: 255 ms a letter at birth
  (about 34 words a minute), gamma 0.75, a 10-minute lead, 721 ms from 21:00 to the death
  (about 12); three thoughts shown in each of movements I to III and one or two in IV at the
  measured costs (thoughts end at 3.7, 5.2, 6.6 / 8.2, 9.8, 11.7 / 14.1, 17.5, 21.4 / 25.5
  minutes, and a cut one at the death).
- **Trade-off.** The world's losses are scenery the visitor cannot always check (a service on a
  board), so the screen dims for real and the readings name each loss. Stopping services on the
  installation machine is restricted to an allowed list and always undone. The freshness guard
  is a change of sampling from request to request; it only pushes away from the last openings
  and leaves temperature, `min_p`, the length and the persona unchanged.
