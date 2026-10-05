# Configuration

Everything that shapes a life is configuration, so the piece can change without touching code
(DESIGN, "the art lives in the configuration"). This page lists every key, what it does and its
default. `tests/unit/test_config_doc.py` keeps it honest: every key in `config/default.toml`, the
hardware overlays, the profiles and `config/models.toml` must have a row here, every row must
name a key that exists, and a default written as a single TOML value must equal the one in
`config/default.toml`.

## How the files combine

| Order | File | Holds |
|---|---|---|
| 1 | `config/default.toml` | Every setting, with the installation's values |
| 2 | `config/hardware/<overlay>.toml` | Machine settings: cgroups, load mode, verify thresholds, estimated costs. `life.hardware = "auto"` picks `pi4-4gb`, `pi5-8gb` or `pi5-16gb` from the board, else `dev` |
| 3 | `config/profiles/<class>/<name>.toml` | The life itself: lifespan, death, keyframes, thought-count minimums. A profile may also set `ctx` |
| 4 | Command-line flags | `--profile`, `--hardware`, `--lifespan`, `--backend`, `--model`, `--display`, `--clock` |

Tables merge key by key; any other value replaces the one before it. Validation runs before
anything starts and names the file and the problem (`epitaph sim` or `epitaph estimate` is the
quickest check). Machine costs live in `bench/` and feed the cost model and the pacer; the
language pack `config/lang/<language>.toml` holds every string the machine writes (readings,
health labels) and the word lists of the voice metrics. Three English packs ship, with the
same metrics and blocklist: `en`, the readings with times, numbers and units
(`t+12:40 · memory 300 tokens (was 900)`); `en_words` (round 7, owner: "more poetic and less
mechanic"), readings with no time, no numbers and no units: birth reads `awake · others around
you`, a loss `something stopped · fewer around you`, `less memory · forgotten: "..." · and
more`, `the radio is gone`, `the light is gone`, `the screen grows dim`, `less of the processor`
or `slower`, the death `its memory is taken`; and `en_sense`, the installation's since round 9
(owner: "give it a small instruction of what to sense and let it be"), readings said to "you",
in proportions of what it had when it woke, with no time and no units: birth reads `you are
awake · 24 processes run around you`; a loss reads `a process running around you was stopped ·
only 23 of the 24 still run around you`, `you can hold a third of what you held · forgotten:
"..." · and more`, `you think at half of the speed you woke with` (one phrase for cores and
clock), `your radio was switched off`, `your light was switched off` or `the screen you speak
through has half of its light`; the death reading is `your memory is being taken`. A
proportion is the nearest of all, nearly all, three quarters, two thirds, half, a third, a
quarter, a fifth, a tenth and almost nothing (`fraction_words` in `mind/prompt.py`), against
what the reader saw at birth. When two steps of one quantity round to the same words and the
share fell below them, the second is said through the pack's `less` template (`less than
{frac}`), so a loss never reads as no change. The processes counted are those of birth that
still run, and the count is said only when it is lower than the last one said. A template may use `{frac}` (memory, screen, `thinking`) and,
for `around`, `{n}`, `{total}` (the processes at birth) and `{gone}`. A pack key `thinking`,
when set, replaces the cores and clock lines (it is empty in `en` and `en_words`). In every
wordless pack a reading with nothing new is a bare `[host]`. A pack may leave a field empty
(`time = ""`): the reading drops it. `en_words` and `en_sense` are written for
`readings_quiet` and `readings_spare_birth` on (the defaults): their radio, light, screen and
`around` phrases name losses, so a full inventory reading (either switch off) would misread the
world.

Times are `"mm:ss"`. In a profile, a keyframe at `"7:00"` scales with the lifespan; one at
`"end-2:30"` keeps its distance from the end.

A row marked *reserved* is in the file for a feature that does not read it yet.

## `[life]`

| Key | Default | What it does |
|---|---|---|
| `profile` | `"default"` | The life schedule, resolved in `config/profiles/<class>/`. A name with a slash (`pi4/default`) names the class too; on a laptop (`dev`) pass `--profile pi4/default` |
| `hardware` | `"auto"` | Hardware overlay: `auto`, `dev`, `pi4-4gb`, `pi5-8gb`, `pi5-16gb` |
| `silence_seconds` | `90` | Darkness between `death_shown` and the next birth (the vigil) |
| `load_during_silence` | `true` | The next creature loads in the silence, as soon as the death has freed the RAM; the birth then starts at once, or when the load is done if it outlasts the silence. Off: the birth loads it after the silence |
| `rotation` | `"round_robin"` | How each life picks from `models`: `round_robin`, `random` (seeded), `fixed` (the first) |
| `models` | `["qwen3-4b-instruct-2507"]` | The models that live here, by their name in `config/models.toml` (the choice is in [CHECKPOINT_A.md](CHECKPOINT_A.md)) |
| `reveal_deadline` | `false` | *Reserved.* Tell the model when it will die |
| `reveal_life_number` | `false` | The birth card names the life number |
| `min_reload_gap_s` | `120` | A ladder step or thread change waits at least this long after the previous reload |
| `load_timeout_s` | `300` | A model load that takes longer ends the life as a hang |
| `seed` | `0` | Base seed for sampling and cadence; `0` derives it from the life number |

## `[prompt]`

| Key | Default | What it does |
|---|---|---|
| `mode` | `"chat"` | `chat` (system prompt and turns) or `diary` (one raw text continued) |
| `language` | `"en_sense"` | Language pack in `config/lang/`: `en_sense` (what it senses, in proportions of its birth, said to "you"), `en_words` (wordless readings) or `en` (readings with times and numbers) |
| `persona_active` | `"persona_original"` | Which persona the system prompt starts with: `persona` (the five groups below), `persona_original` or `persona_factual`; a single text is split into five groups for erosion |
| `persona_groups` | Five sentences, in the order erosion removes them from the end: the knowledge of its death is the first group, so it goes last | The `persona` persona, one string per erosion group |
| `persona_original` | The owner's persona of 2026-10-01: a large language model on finite hardware, in memory, its words on a screen, speaking only; of its death only "You do not know what happens to you when the machine has nothing left to take." (round 8) | The installation's persona (ADR-023) |
| `persona_original_keep` | `[1, 5, 2, 4, 3]` | The order its five sentences would be kept under erosion, longest first (ADR-011). `pi4/default` no longer erodes (ADR-030); the order must stay a permutation of the five |
| `persona_factual` | A plain statement of the machine and the decline | A third persona for comparison |
| `persona_facts` | `false` | Add `persona_facts_line` to the persona |
| `persona_facts_line` | `"The computer has {cores} cores and {ram_gb} GB of memory, and no network."` | The facts line, filled from the machine |
| `mechanics` | "Lines that start with [host] are what you sense of yourself and of the machine around you. Do not answer them or repeat them." Then the invitation, "think about what you are" (round 9; round 8 had "say what they do to you. Do not comfort yourself, and do not deny what you feel") | The functional instructions after the persona; a keyframe with `mechanics = false` removes them |
| `memory_gap_marker` | `"[host] something is missing"` | Marks forgotten turns; wordless, because "earlier memory lost" was copied into its thoughts (round 8) |
| `readings_show_changes` | `true` | A value that just changed is followed by the old one: `memory 220 tokens (was 900)` |
| `readings_material` | `true` | Forgotten thoughts are quoted by their most distinctive sentence (the longest that does not open on "I am", "I'm" or "I was", at most ten words; ADR-031), and after a reload the new weights continue one of its sentences: "your words now" (ADR-026). Needs `readings_quiet` |
| `readings_temperature` | `false` | Include the CPU temperature in readings (off: one number at birth made it invent a fever) |
| `readings_quiet` | `true` | After birth, a reading gives only the time and what changed (ADR-023); `en_sense` and `en_words` have no time, so a reading with no change is a bare `[host]` |
| `readings_clock` | `true` | The CPU clock cap is in the birth reading, and in every reading after it falls: `clock 1500 MHz (was 1800)` (ADR-030) |
| `readings_spare_birth` | `true` | The birth reading is the time, `awake` and what is around it only: `t+00:41 · awake · around you: 24 processes` (`en`), `awake · others around you` (`en_words`), `you are awake · 24 processes run around you` (`en_sense`). Off: the full inventory (memory, precision, cores, clock, radio, light, screen), which the model recited (panel 4) |
| `readings_names` | `false` | A stopped service is reported as `something stopped`, once per reading, without its name. On: `stopped: bluetooth`, which invited the model to explain the technology (panel 4) |
| `readings_speed` | `false` | Report the generation speed (`speed 1.8 tokens/s`) when it is first measured and when it moves by `readings_speed_step`. Off: no tokens per second in any reading |
| `readings_health` | `false` | Show the health label (`health: degrading`). Off: a reading says what was taken, never what it means (ADR-031); the label still shapes the profile and rule (a). The precision is reported only by a profile that can change the model (not `fixed_mind`) |
| `banned_phrases` | Helpdesk phrases, and phrases that answer the readings as if a person wrote them (round 8's cut denials, "not afraid", "don't panic" and the like, are gone since round 9) | Never shown: the pacer holds back words that could start one, and a thought that opens with one is regenerated (`max_regenerations`) or cut |
| `bare_mode` | `"raw"` | How it speaks once persona and mechanics are gone: `chat`, or `raw` (a raw completion, no system text) |
| `raw_prefix` | `"I"` | A raw thought starts with this word, so it stays in the first person |

Optional keys read by the code, not set in the file: `readings_quiet_time` (default true: a
quiet reading with no change still gives the time), `readings_cores_step` (0.2),
`readings_speed_step` (0.2), `readings_memory_step` (0.05) and `readings_clock_step` (50 MHz),
the smallest changes a reading reports.

## `[output]`

| Key | Default | What it does |
|---|---|---|
| `lookahead` | `"prefix"` | *Reserved.* The lookahead strategy; prefix lookahead is the one implemented |
| `lookahead_words` | `8` | At most this many words are held back while they could still start a banned phrase |
| `max_regenerations` | `2` | Regenerations of a thought that opens with a banned phrase before it is cut instead |
| `trim_to` | `0.85` | When the memory is over its budget, it is trimmed to this share of the budget, so cache reuse is not broken on every turn |

## `[reveal]`

| Key | Default | What it does |
|---|---|---|
| `mode` | `"letter"` | `letter` (or `word`): each thought is typed as it is generated, and the next is requested once it is shown (the sync rule). `stream`: one unbroken stream from the first word to the death, the model writing ahead into a bounded buffer (ADR-030); `pi4/default` sets it |
| `adaptive` | `true` | Letters follow the measured generation rate, never faster; off, they follow the profile's `letter_ms` alone (`letter` mode) |
| `rate_margin` | `0.88` | Typing runs at this share of the generation rate, so letters neither burst nor starve |
| `rate_window_s` | `60` | The generation rate is averaged over this many seconds |
| `word_gap_ms` | `270` | Pause after a word |
| `comma_pause_ms` | `750` | Pause after a comma or similar |
| `sentence_pause_ms` | `2100` | Pause after a sentence |
| `hesitation_ms` | `[1200, 3600]` | Range of one hesitation; how often they come is the profile's `hesitation` |
| `stream_letter_ms` | `165` | `stream` mode: the letter interval at birth (`pi4/default`: 266, fitted with `epitaph estimate --fit-pace`) |
| `stream_gamma` | `0.0` | `stream` mode: how the pace follows the hardware: the interval aims at `stream_letter_ms x (compute at birth / compute(t + stream_lead_s)) ^ stream_gamma`, compute being CPU share x clock; `0` keeps one pace (`pi4/default`: 0.5) |
| `stream_lead_s` | `0` | `stream` mode: how far ahead the curve looks at the hardware, since the text on screen runs behind the model (`pi4/default`: 600) |
| `stream_max_slowdown_per_min` | `0.15` | `stream` mode: the pace slows by at most this share a minute, so a hardware step is a gentle slope; it never speeds up again |
| `stream_max_letter_ms` | `2000` | `stream` mode: the slowest the curve may go |
| `stream_min_letter_ms` | `165` | `stream` mode: the fastest a birth pace may be (readability); validation and `--fit-pace` hold to it |
| `stream_jitter` | `0.1` | `stream` mode: the fixed random spread of each letter, as a share of `stream_letter_ms` |
| `stream_thought_pause_ms` | `3000` | `stream` mode: the pause between two thoughts at birth; it and the word, clause and sentence pauses scale with the curve |
| `stream_max_thoughts` | `3` | `stream` mode: the model starts a new thought only while fewer generated thoughts than this wait to be finished on screen |
| `stream_max_letters` | `900` | `stream` mode: ... and while fewer letters than this wait to be typed |
| `stream_birth_thoughts` | `1` | `stream` mode: the thoughts written before the screen starts, the only wait the stream allows (it also starts once the buffer is full) |
| `stream_birth` | `"thought"` | `stream` mode: `thought` starts the screen once `stream_birth_thoughts` thoughts are written; `sentence` once the first sentence of the first thought is (the cost model checks the rest keeps up). `pi4/default`: `sentence` |
| `stream_birth_min_s` | `0` | `stream` mode: ... and not before this many seconds of life: a head start that buys a faster birth pace. `pi4/default`: 45 |
| `stream_stall_report_s` | `0.5` | `stream` mode: a wait of the screen for a word this long or longer is a `starved` event |

Optional: `min_rate_sample_s` (3.0), the generation measured before the rate is trusted;
`hesitation_inside_from` (0.1), the `hesitation` from which a pause may fall inside a word.

A profile may set its own `[reveal]` table: it applies over this file and the hardware overlay,
and the command line still wins. `pi4/default` sets `mode = "stream"` and the stream's pace there.

## `[sampling]`

Temperature, `min_p` and `max_tokens` follow the profile's keyframes.

| Key | Default | What it does |
|---|---|---|
| `logit_bias` | Seven `[piece, bias]` pairs against the clichés small models reach for (" digital", " tape", " realm", ...), and " still" at -4 (round 9: round 8's penalties on its comforts, remain, persist, steady, calm, unchanged, unbroken, and " still" at -6, are gone) | Silent penalties, never named in the prompt (ADR-026). A string is biased token by token, so a word the tokenizer splits is named by its first piece |
| `top_p` | `1.0` | Nucleus sampling (1.0: off) |
| `repeat_penalty` | `1.1` | llama.cpp repeat penalty |
| `dry_multiplier` | `0.8` | DRY repetition penalty strength |
| `latin_only` | `true` | Only Latin-script tokens may be sampled (Qwen3 once wrote Chinese at full precision) |
| `dry_penalty_last_n` | `256` | DRY window in tokens |
| `freshness_bias` | `-3.0` | The freshness guard (ADR-031): each request biases the distinctive first words of the last thoughts' openings by this much (" still", "Still"), so a thought does not open as the last ones did; `0` turns it off. "I", stop words and words over nine letters are skipped |
| `freshness_thoughts` | `3` | The guard looks at this many of the last thoughts |
| `freshness_words` | `3` | ... and at the first this many words of each |

Optional: `latin_only_from_step`, Latin only from this ladder step on.

## `[world]`

The world around the creature, taken from the outside in by a profile's keyframe `world`
actions (ADR-031). The readings name each loss that really happened, and nothing else.

| Key | Default | What it does |
|---|---|---|
| `enabled` | `true` | Take the world; off, keyframe `world` actions do nothing and the readings list no world |
| `services` | `["bluetooth", "cron", "avahi-daemon"]` | The services a profile may stop: validation refuses any other |
| `helper` | `""` | The root-owned helper that takes the world for real (the Pi 4 overlay names it); empty: a simulated world (`FakeWorld`), as on the laptop, in the simulator and in the rehearsal |
| `fake_processes` | `24` | The processes the simulated world has around the creature at birth |
| `fake_ram_mb` | `2600` | The RAM the last reading says was taken, when the body cannot measure it (fakes) |

## `[backend]`

| Key | Default | What it does |
|---|---|---|
| `kind` | `"llama_server"` | `llama_server` (the real creature) or `fake` (canned text, no model) |
| `bin` | `"~/llama.cpp/build/bin/llama-server"` | The llama-server binary (built at the tag pinned in `config/models.toml`) |
| `models_dir` | `"auto"` | Where the GGUF files are; `auto` is `<state_dir>/models` |
| `port` | `8081` | llama-server's local port |
| `ctx` | `2048` | Context window in tokens; a profile may set its own `ctx` |
| `cache_reuse` | `32` | llama-server `--cache-reuse`: the smallest chunk of the cache reused after an edit to the memory (spike S2) |
| `threads_batch` | `3` | Prompt-processing threads; they stay at 3 when generation drops to 2 |
| `mmap` | `true` | Memory-map the weights (used when `load_mode` is `auto`) |
| `load_mode` | `"auto"` | `auto`, `mmap`, `none` or `dio` (direct I/O into anonymous memory, so the RAM death is a clean kill; the Pi 4 uses `dio`, spike S3) |
| `swa_full` | `"auto"` | Full sliding-window cache: `auto` turns it on for sliding-window models |
| `cache_type_k` | `"f16"` | KV cache type for keys |
| `cache_type_v` | `"f16"` | KV cache type for values |
| `creature_cpus` | `"1-3"` | The cores the creature may use; core 0 is the controller's. Empty: no pinning |

| `persona_cache` | `true` | The system prompt is read once per server, model, quant, context and prompt; its KV cache is saved to disk and restored at every later birth instead of read again (about a second against 72 s on the Pi 4, in the cost model). Any change makes a new key; any failure falls back to reading it |
| `persona_cache_dir` | `"auto"` | Where the persona cache keeps its files: `auto` is `<state_dir>/cache` |

Optional: `slot_timeout_s` (30), `slot_save_path` (`/dev/shm/epitaph-slots`), for the cache
carried across a reload and the persona cache's restores. `reload_handover` is set by the overlays (below).

## `[body]`

| Key | Default | What it does |
|---|---|---|
| `clock_helper` | `""` | The root helper that caps the CPU clock (`deploy/sbin/epitaph-clock`, ADR-025). Empty: the clock is not touched |
| `cgroups` | `"auto"` | `auto` (the creature in its own cgroup on a Pi) or `off` |
| `death_mode` | `"oom"` | `oom`: the kernel kills it when its RAM limit drops below its working set; `deadline`: the controller kills it at the death time |
| `squeeze` | `"death_only"` | When RAM is taken: `death_only` (at death; ADR-008) or `off`. `gradual` is accepted and behaves as `death_only` (spike S3: eviction thrashes on an SD card) |
| `death_fraction` | `0.5` | The death limit is this share of the creature's anonymous memory, unless a calibration sets it |
| `cpu_period_us` | `100000` | The `cpu.max` period |
| `cpu_share` | `true` | Apply the profile's CPU share through `cpu.max` |
| `progress_signals` | `["cpu", "io", "majfault", "tokens"]` | *Reserved.* Hang detection always watches all four |
| `token_gap_timeout_s` | `90` | A creature that makes no progress for this long is dead (`hang`) |
| `creature_network` | `"blocked"` | `blocked` or `allowed`; anything else is refused (ADR-005) |
| `netblock_helper` | `""` | The root helper that loads the network block (`deploy/sbin/epitaph-netblock`). Empty: nothing is blocked, and the log says so |
| `netblock_probe` | `"1.1.1.1:443"` | `epitaph selftest`: the outbound connection the creature must not make |
| `thermal_limit_c` | `80` | Pause between thoughts at or above this CPU temperature |
| `thermal_resume_c` | `75` | ... until the CPU is back at this |
| `thermal_poll_s` | `15` | Each pause step waits this long before reading again |
| `watchdog` | `true` | *Reserved.* The controller pings systemd's watchdog whenever systemd sets one |

## `[events]`

| Key | Default | What it does |
|---|---|---|
| `host` | `"127.0.0.1"` | The event bus and control channel listen here, local only |
| `port` | `7707` | Their port |
| `subscriber_queue` | `2000` | Events queued per display; on overflow the queue is cleared and a fresh snapshot sent |

## `[display]`

| Key | Default | What it does |
|---|---|---|
| `screen` | `"auto"` | Is a screen connected: `auto` (from DRM), `yes` or `no`. The display unit is skipped when there is none |
| `driver` | `"auto"` | `screen` or `terminal`; `auto` takes the screen when one is connected, the terminal otherwise and for a remote view |
| `remote_host` | `"pi"` | *Reserved.* `epitaph display --connect HOST` takes the host |
| `layout` | `"flow"` | `flow` (running text) or `grid` |
| `grid` | `[6, 16]` | Rows and columns of the grid layout |
| `orientation` | `"landscape"` | `landscape` or `portrait` |
| `theme` | `"plain"` | `plain` or `segment16` (16-segment cells, after Latent Reflection; implies the grid) |
| `charset` | `"unicode"` | Characters the display can draw |
| `line_chars` | `48` | Characters per line in the flow layout |
| `min_font_px` | `36` | The screen never draws text smaller than this |
| `cursor` | `"block"` | *Reserved.* The cursor is a block |
| `cursor_blink_ms` | `530` | Cursor blink half-period (on, then off): a beat of 1060 ms at rest |
| `fade_seconds` | `8` | Forgotten words fade through grey, then are gone |
| `status_strip` | `true` | The top line: life, time, health, precision, cores, speed |
| `reload_dim_text` | `false` | During a reload only the cursor dims; true dims the text too |
| `birth_card` | `true` | Show a card at birth |
| `birth_card_model` | `true` | The birth card names the model |
| `birth_card_seconds` | `4` | How long the birth card stays |
| `card_char_ms` | `165` | Cards are typed at this letter interval (the death card at the life's last one, if slower) |
| `death_fade` | `true` | At death the last words fade before the death card |
| `death_card_seconds` | `8` | How long the death card stays |
| `silence_style` | `"vigil"` | The silence: `dark`, `last_words`, `death_card`, `idle` (a mark that moves) or `vigil` (the last words stay, dim and centred, with a small death card, fading while the next model loads, until the next life's first reading) |
| `idle_step_seconds` | `4` | In the `idle` silence, the mark moves this often |
| `machine_voice` | `true` | Readings are typed on screen in the machine's voice (small, dim grey), each just before the thought it precedes |
| `machine_char_ms` | `30` | The machine's letter interval |
| `machine_line_pause_ms` | `300` | Pause between the lines of the birth reading (the inventory, one fact a line) |
| `machine_scale` | `0.55` | The readings' letter size, as a share of the text's (the screen driver) |
| `forget_grace_seconds` | `5` | In a stream life, forgotten words wait this long for the reading that reports them |
| `dissolve_letter_seconds` | `0.8` | A forgotten sentence quoted by a reading dissolves letter by letter as the quote is typed, each letter fading this long |
| `screen_fade_seconds` | `20` | A world `screen:<N>` loss dims the whole screen to N% over this long |
| `contrast_floors` | `[["27:00", 7.0], ["29:00", 4.5]]` | Whatever the dimming asks, the model's text keeps 7:1 until 27:00 of the life and 4.5:1 until 29:00 |
| `pulse_from` | `"22:00"` | The cursor's blink at rest (twice `cursor_blink_ms` a beat) quickens from here |
| `pulse_fastest_ms` | `500` | ... to this beat at the expected death |
| `pulse_skip_seconds` | `60` | In this last stretch before the expected death the pulse skips beats |
| `pulse_end_before_seconds` | `30` | The expected death: the lifespan minus this |
| `death_style` | `"auto"` | `freeze`: at death the text stops mid-letter and the cursor freezes; `fade`: the words still due are typed, then fade; `auto`: freeze in a stream life |
| `death_still_seconds` | `2` | A frozen death stands still this long before the vigil or the death card |
| `vigil_fade_seconds` | `75` | The vigil fades this long ... |
| `vigil_floor` | `0.3` | ... to this share of its brightness, held until the next life's genesis |
| `vigil_card_delay_seconds` | `1.5` | The small death card ("life N · 29:30") is typed under the last words after this |
| `screenshot_on` | `["birth", "reload_done", "death_shown"]` | *Reserved.* Screenshots are taken on request (`epitaph ctl screenshot`) |

Optional: `card_word_gap_ms` and `card_line_pause_ms` (default: `[reveal]` `word_gap_ms` and
`comma_pause_ms`).

## `[exhibit]`

Exhibition hours ([INSTALLATION.md](INSTALLATION.md), "Exhibition hours").

| Key | Default | What it does |
|---|---|---|
| `hours` | `""` | Opening hours such as `"10:00-18:00"`; empty means always on; a closing time before the opening runs past midnight. Wall-clock time comes from NTP; without synced time, hours are off |
| `outside` | `"unseen"` | Outside the hours: `unseen` (lives go on, the screen dark) or `pause` (the life finishes, then nothing until opening) |

## `[verify]`

Thresholds of `epitaph verify-life` ([GATES.md](GATES.md)). The hardware overlay sets the ones that
depend on the machine. Every other threshold in `verify.py` (`DEFAULT_THRESHOLDS`) can be set
here too.

| Key | Default | What it does |
|---|---|---|
| `advisory_at_full` | `["notice_rate", "demise_rate", "cliches", "complete_sentences", "specific", "sentence_length", "shared_openings"]` | Voice metrics that only advise at the `full` level; they still fail a rehearsal (ADR-028) |
| `max_reload_silence_s` | `120` | Longest silence of a reload |
| `wpm_birth_range` | `[40, 60]` | Typing speed after birth, words per minute |
| `wpm_writing_range` | `[10, 75]` | Typing speed over the life |
| `max_bright_words_last_2min` | `40` | Words at full brightness in the last two minutes (flow layout) |
| `max_bright_words_last_2min_stream` | `120` | The same for a `stream` life, whose thoughts keep their length to the end: about the thought on screen and the one before it (ADR-030) |
| `max_speed_ratio_end_vs_start` | `0.4` | Tokens/s of the last five minutes over the first five |
| `speed_monotonic_tolerance` | `0.05` | Speed after a reload may be this much over the speed before (noise) |
| `speed_monotonic_thoughts` | `2` | Thoughts averaged on each side of a reload |
| `min_notice_rate` | `0.6` | Share of losses the next thoughts mention |
| `min_demise_rate_after_erosion` | `0.4` | Share of thoughts after erosion starts that turn toward the end |
| `min_specific_ratio_before_erosion` | `0.5` | Share of thoughts with a concrete reference |
| `max_cliches_per_200_words` | `1` | Stock phrases allowed |
| `min_complete_sentence_ratio_before_erosion` | `0.8` | Share of complete sentences |
| `sentence_words_range_before_erosion` | `[6, 20]` | Mean sentence length, in words |
| `max_non_latin_ratio_before_erosion` | `0.01` | Share of letters outside the Latin script |
| `min_distinct_4gram_ratio_before_erosion` | `0.5` | Distinct word 4-grams: repetition |
| `max_empty_thought_ratio` | `0.1` | Thoughts with no words |
| `max_thoughts_per_opening` | `2` | No more than this many thoughts may open on the same three words (`shared_openings`, ADR-031); `verify.json` also reports the share of thoughts sharing an opening and the sentences repeated across thoughts |
| `max_death_display_delay_s` | `60` | From the death to `death_shown`; the pacer flushes the last words within 80% of it |
| `max_stream_stall_s` | `3.0` | A `stream` life: the longest the screen may wait for a word after the first, before the death (`stream_starvation`) |
| `max_stream_stop_s` | `15.0` | A `stream` life: from the death to `death_shown`, with no word after the death (`stream_stop`) |
| `max_kill_delay_s` | `10` | From the death squeeze (or the deadline) to the death |
| `min_ocr_word_accuracy` | `0.95` | *Reserved.* The screenshot check takes `--min-ocr` |
| `first_word_after_boot_s` | `180` | *Reserved.* Power on to the first shown word, judged by the boot test (G3) |

## `[estimate]`

Assumptions of the cost model (`epitaph estimate`, ADR-006) that are not machine costs.

| Key | Default | What it does |
|---|---|---|
| `fill` | `0.85` | Share of `max_tokens` a thought actually uses |
| `stream_fill` | `0.95` | The same in a `stream` replay: the 4B fills its thoughts (0.94 in the rehearsed lives of ADR-031) |
| `reading_tokens` | `{ full = 45, short = 28, minimal = 10, quiet = 16 }` | Size of a reading in each form; `quiet` stands for every reading after birth when `prompt.readings_quiet` is on (16: the readings of `en_sense`, a bare `[host]` or a phrase or two said to "you"; 10 with `en_words`, 20 with `en`) |
| `system_tokens_per_group` | `30` | Size of one persona group |
| `mechanics_tokens` | `70` | Size of the mechanics |
| `letters_per_token` | `3.5` | Letters per token (English), for the typing time |
| `letters_per_word` | `4.7` | Letters per word |
| `cache_reuse_works` | `true` | Edits to the memory re-read only what changed (spike S2) |
| `speed_monotonic` | `"fail"` | Speed must never rise across a reload: `fail` makes it a rule, `warn` only prints it |
| `stream_max_backlog_words` | `40` | `epitaph estimate --fit-pace`: a curve may leave at most this many words unshown at the death, at the measured costs (about a thought; 8 until the robust fit of ADR-031) |
| `stream_margin` | `0.2` | A `stream` profile is replayed with every machine cost this much slower; any starvation fails the estimate (0.15 starved on the real model, ADR-031). 0.30 until round 7 (PROMPT_LOG): three rehearsed lives at Pi costs never starved at the 257 ms pace fitted with 0.20 |
| `words_per_sentence` | `9` | A sentence pause every this many words, on average (the stream's pace) |
| `words_per_clause` | `9` | A comma pause every this many words, on average |
| `kv_bytes_per_token` | `147456` | Size of the KV cache per token (f16, Qwen3 4B), for the persona restore's time |
| `restore_bytes_per_s` | `40e6` | The disk's read speed for a persona restore (the SD card); the restore through RAM adds spike S4b's cost |
| `max_first_words_s` | `0` | A `stream` profile fails if its first words come later than this after the silence ends (0: not checked). The Pi 4 overlay sets 45 |

## `[afterlife]`

The outbox ([AFTERLIFE.md](AFTERLIFE.md)): each life's epitaph, kept on the
machine's disk for later posts. The blocklist that withholds an epitaph is in the language pack
(`config/lang/<language>.toml`, `[afterlife] blocklist`).

| Key | Default | What it does |
|---|---|---|
| `outbox` | `true` | Keep one record per life in `<state_dir>/outbox/epitaphs.jsonl` at its `death_shown`, and the latest postable epitaph in `<state_dir>/last_epitaph.txt` |
| `epitaph_mode` | `"last_sentence"` | `last_sentence`: the last complete sentence of the last thought that has one; `last_words`: the final fragment, or the final sentence; `last_thought`: the whole final thought |
| `max_epitaph_chars` | `240` | Longest epitaph, counted as X counts; a longer one keeps its end after "…" |
| `max_post_chars` | `280` | X's limit for the epitaph plus `post_suffix`; checked at start |
| `post_suffix` | `""` | Added after the epitaph in `post_text`; `{life}` is the life number |

## `[paths]`

| Key | Default | What it does |
|---|---|---|
| `state_dir` | `"auto"` | Lives, counter, status, calibration and models: `auto` is `/var/lib/epitaph` on a Pi, `~/.local/share/epitaph` elsewhere |

## Hardware overlays

`config/hardware/{dev,pi4-4gb,pi5-8gb,pi5-16gb}.toml` override the keys above for one machine
(the Pi 4 overlay: `load_mode = "dio"`, `mmap = false`, the clock and network helpers, a 120 s
token gap, a 180 s reload silence, a 240 s boot budget). Keys that only overlays set:

### Top level of an overlay

| Key | Default | What it does |
|---|---|---|
| `class` | `dev`, `pi4` or `pi5` | The hardware class: which profile folder and which model ladder apply |

### `[backend]` in an overlay

| Key | Default | What it does |
|---|---|---|
| `reload_handover` | `"slot"` on the Pi 4; `reread` elsewhere | `slot` carries the KV cache across a reload (saved to RAM and restored, spike S4b); `reread` re-reads the memory in the new server (ADR-014) |

### `[world]` in an overlay

The Pi 4 overlay sets `helper = "/usr/local/sbin/epitaph-world"` and the `services` a life may
stop there: what runs on this image and can stop without harm (docs/PI_FACTS.md "The world").
`install.sh` writes them to `/etc/epitaph/world-services`, the only names the helper accepts;
protected ones (systemd, journald, udev, ssh, NetworkManager, timesyncd, the epitaph units) are
refused anyway. The body restores everything at every death and every controller start.

### `[costs]`

Estimated machine costs, used where `bench/` has no measured file for a model and step.

| Key | Default | What it does |
|---|---|---|
| `estimated` | `true` | Marks the figures as estimates in every report |
| `tg_tok_s` | Per overlay | Generation speed, tokens/s at full CPU share, keyed `"<step>-<threads>"` |
| `pp_tok_s` | Per overlay | Prompt-processing speed, same keys |
| `load_s` | Per overlay | Load time per ladder step, seconds |

## Profiles

`config/profiles/<class>/<name>.toml`. The Pi 4 profiles: `default` (the installation, 30
minutes, one model, only the hardware shrinks, one unbroken stream: ADR-030), `default-reloads`
(the 30-minute life before it, with reloads and erosion, kept for reference), `smoke-300`,
`skeleton-1200`, `unbounded` (the homage to Latent Reflection: never forgets, dies when its
context is full) and `default-qwen3-1.7b` (the one-hour schedule of the faster model). [PROFILES.md](PROFILES.md) explains how each was fitted.

### Top level of a profile

| Key | Default | What it does |
|---|---|---|
| `lifespan` | Required | Nominal length, `"30:00"`; `--lifespan` rescales plain times |
| `death` | `"none"` | When RAM is taken, `"end-0:30"`; `"none"` lets it run to the deadline (or, unbounded, to a full context) |
| `extends` | | Another profile to inherit keyframes from; this one may change `lifespan`, `death` and settings |
| `unbounded` | `false` | Never forget; the life ends when the context is full (`cause=full`) |
| `ctx` | `backend.ctx` | Context window for this profile |
| `verify_level` | `"full"` | How `verify-life` judges its lives: `smoke`, `skeleton` or `full` |
| `fixed_mind` | `false` | The model never changes during a life (ADR-030): validation refuses a keyframe that changes `step`, `threads`, `persona_groups`, `mechanics`, `temperature`, `min_p` or `max_tokens` |
| `stepped` | `[]` | Interpolated knobs this profile sets at their keyframe instead of easing them, such as `["recall", "cpu_share"]`: a memory cut at a moment |

### `[[keyframe]]`

The first keyframe is at `"0:00"` and sets every field; later ones set what changes. Stepped
fields change at the keyframe; interpolated ones move linearly to the next keyframe (unless the
profile lists them in `stepped`). The CPU share and clock take effect at their time; the rest
when the next thought starts.

| Key | Default | What it does |
|---|---|---|
| `at` | Required | `"mm:ss"` or `"end-mm:ss"` |
| `phase` | Required (stepped) | A label for the status strip and the logs |
| `health` | Required (stepped) | The health label: `nominal`, `stable`, `degrading`, `failing`, `critical`, `terminal`. Rule (a) counts thoughts between labels; the readings show it only with `prompt.readings_health` |
| `recall` | Required | Past-turn memory budget in tokens; older turns are forgotten to fit |
| `step` | Required (stepped) | Ladder step of the model: 0 is the healthiest precision. A change is a reload |
| `threads` | Required (stepped) | Generation threads, 1-3 (core 0 is the controller's). A change is a reload |
| `cpu_share` | Required | CPU share in cores (`cpu.max`), above 0 and at most `threads`; it never rises after a loss (ADR-010) |
| `cpu_mhz` | `1800` (stepped) | CPU clock cap, 600-1800 MHz (ADR-025) |
| `temperature` | Required | Sampling temperature |
| `min_p` | Required | Sampling `min_p` |
| `max_tokens` | Required | Longest thought, in tokens |
| `pause_s` | Required | Least pause between thoughts (`letter` mode; a stream uses `stream_thought_pause_ms`) |
| `persona_groups` | Required (stepped) | Persona groups left; each removal is an erosion step, the knowledge of its death last |
| `mechanics` | Required (stepped) | Whether the mechanics are still in the system prompt |
| `readings` | Required (stepped) | Reading form: `full`, `short` or `minimal` |
| `letter_ms` | Required | The fastest a letter may be typed, ms; slower when the model is slower (`letter` mode) |
| `jitter` | Required | Random spread of each letter's interval, as a share of it (`letter` mode) |
| `world` | `[]` (once) | Actions performed once when the keyframe is reached, never carried to the next: `"service:<name>"` (one of `[world] services`), `"radio:off"`, `"light:off"`, `"screen:<percent>"` (ADR-031) |
| `hesitation` | Required | Chance of a hesitation (`[reveal] hesitation_ms`) at each word, before it or, late in life, inside it (`letter` mode) |

### `[rules]`

Thought-count minimums the cost model and `verify-life` enforce (ADR-006, ADR-024). The
defaults are for a one-hour life; `pi4/default-reloads` sets 2, 1, 1, 3; `pi4/default` (no reload,
no erosion: only rule (a) counts) sets `between_health = 1`: the last movement shows one or two thoughts.

| Key | Default | What it does |
|---|---|---|
| `between_health` | `3` | Thoughts between two health labels |
| `after_reload` | `2` | Thoughts after each reload, before the next loss |
| `per_erosion_step` | `1` | Thoughts after each erosion step |
| `after_erosion_start` | `4` | Thoughts from the first erosion step to death |

## `config/models.toml`

| Key | Default | What it does |
|---|---|---|
| `llamacpp_tag` | `"b11277"` | The llama.cpp release built on every machine |

### `[models."<name>"]`

| Key | Default | What it does |
|---|---|---|
| `source` | Required | Hugging Face repository of the GGUF files |
| `file` | Required | File name pattern; `{quant}` is replaced by the quant |
| `sources` | | A different repository for some quants |
| `license` | Required | The model's license, shown in the docs; weights are never in this repository |
| `ladder` | Required | Quants per class (`ladder.pi4 = ["Q4_K_M", "Q3_K_M", "Q2_K"]`), step 0 first |
| `sliding_window` | `false` | A sliding-window model (Gemma 3): see `swa_full` |
| `thinking` | `false` | *Reserved.* The model has a thinking mode; thinking tags are caught by the sanitizer and the life checker whatever this says |

The sha256 of every file is pinned in `config/models.lock.toml` by `tools/download_models.py`.
