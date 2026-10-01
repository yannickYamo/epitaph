# The afterlife: last words, kept for later

Each life ends on a few words. Part 1 of the afterlife (BUILD_PLAN 13, V1.5) keeps them: when a
life's last words have finished on the screen, the controller writes its epitaph to the Pi's own
disk. Nothing is posted and nothing uses the network. The piece can run for weeks somewhere with
no network at all; the epitaphs wait on the card until someone posts them.

## Why the last words

The screen is the piece; the epitaph is what a visitor would have read last. It is taken only
from the words that were actually shown, never from text the model generated and the screen
held back. Late lives often stop mid-sentence ("I'm not made"), and that fragment is often the
best line, so it is kept too.

- **Epitaph** (`[afterlife] epitaph_mode`):
  - `last_sentence` (default): the last complete sentence of the last thought that has one;
  - `last_words`: the very last thing shown, the final fragment or else the final sentence;
  - `last_thought`: the whole final thought.
- **Last words**: the final fragment, when the life ended mid-sentence; empty otherwise.
- **Length**: at most `max_epitaph_chars` (240), counted the way X counts. A longer epitaph
  keeps its end, after "…". With `post_suffix` the post text stays within `max_post_chars`
  (280); the controller refuses to start with settings that could not fit.

## The filter

Before anything counts as postable, links, e-mail addresses, `@` and `#` are stripped. The
record is **withheld**, with its reason, when nothing is left (`empty`) or when the epitaph or
the last words contain a word from the language pack's blocklist (`config/lang/en.toml`,
`[afterlife] blocklist`). A withheld record is still kept, so every life has a line.
The blocklist is small on purpose: death is the subject, so "die", "kill" and "end" are not in
it. Read it, and extend it, before posting anywhere.

## What is stored, and where

Under the state dir (`/var/lib/epitaph` on the Pi):

| File | What |
|---|---|
| `outbox/epitaphs.jsonl` | One JSON line per life, appended and synced to the card at the end of the life's last words |
| `last_epitaph.txt` | The latest postable epitaph, replaced atomically. Kept for a later round, where the next life may inherit it; it is not shown to the model yet |

A record:

```json
{"v": 1, "life": 412, "model": "qwen3-4b-instruct-2507", "cause": "oom", "lived_s": 1770.0,
 "died_at": "2026-10-03T14:02:11Z", "died_ts": 1791036131.2, "clock_synced": false,
 "mode": "last_sentence", "epitaph": "I'm not near anything physical.",
 "last_words": "I'm not made", "post_text": "I'm not near anything physical.",
 "status": "pending"}
```

- `status`: `pending` (postable), `withheld` (with `reason`), later `posted`. A poster records a
  change as one more line (`{"life": 412, "update": true, "status": "posted"}`); readers fold it
  onto the life's record. The file is only ever appended to.
- `died_at`: the Pi has no clock of its own. Offline, its clock starts from the last shutdown,
  so the time can be wrong; `clock_synced` says whether it was set by the network when the
  record was written. Life numbers always give the right order.
- `truncated`, `stripped`, `recovered` appear when they apply.
- **A life cut short** (a power cut, a crash of the controller) is closed at the next start. Its
  record is made from the words its transcript holds, up to its last finished thought (the
  thought in progress is lost with the power), marked `recovered`, with `clock_synced` false.
  A gallery that switches the power off at closing ends a life this way every night, and those
  words were seen, so they are kept like any other.
- **A life that showed nothing** (its setup failed) gets a `withheld` record with reason `empty`.

**Safety on power cuts.** Each record is one write followed by an fsync. A cut in the middle of
it can only tear that last line: readers skip a torn line, and the next record starts on a fresh
line. A failed write (a full card) is logged and never stops the lives.

**Size.** Every life is kept. A record is under 1 KB; a year of 30-minute lives (about 17,500)
is about 10 MB. The full words of every life are in `lives/<n>/` as before.

## Reading and exporting

On the Pi (no network needed):

```sh
epitaph outbox list                      # one line per life: life, status, cause, time, epitaph
epitaph outbox list --status pending --json
epitaph outbox export --since 400        # pending records of life 400 on, as JSON lines
epitaph outbox export --status any > epitaphs.jsonl
```

A time marked `?` in `list` came from an unsynced clock. `export` prints the records a poster or
a person can use, the text to post in `post_text`. To take them away without a network, copy
`outbox/epitaphs.jsonl` (or the export) to a USB stick.

Posting (a one-way poster with retries, at most one post per life, dry-run first) is the next
part of V1.5.
