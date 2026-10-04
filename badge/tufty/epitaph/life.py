# epitaph on a small chip: one life, on any board that runs MicroPython with ulab (or Python
# with numpy, on a laptop).
#
# The rules of the Raspberry Pi installation: the model never changes; only the hardware
# shrinks. Every [host] reading reports something the board really did a moment before, and
# the model reads it and answers: its light switched off, its memory window cut (and what fell
# out of it), its CPU clock lowered, its screen dimmed, and at the end its RAM taken until the
# next thought cannot be allocated.
#
# Nothing here knows a board. A port subclasses `Board` (what the hardware can do) and gives
# an `out` to write on (see terminal.py for the smallest one, and __init__.py for the Tufty
# badge). What a board cannot do is skipped, and the model is never told of it.

import gc
import math
import random
import time

import tinyllama
from tinyllama import np

LIFE_S = 600  # ten minutes of losses; it dies once it has answered the last reading
LAST_WORDS_S = 180  # ... which a slow clock may take this long to read; past it, a deadline
TEMPERATURE = 0.4  # never changes; a 260K model spells best cool
MIN_THOUGHT = 140  # tokens before a paragraph may end (at a full stop)
MAX_THOUGHT = 260  # past it, the paragraph ends at its next full stop
HARD_STOP = 320  # tokens, whatever comes
LAST_THOUGHT = (12, 30, 60)  # the same three for its answer to the last reading: it is short
SLOWER = 0.25  # each word waits a quarter of its own thinking time more
NEWLINE = 13  # the byte token for "\n"
BYTE_TOKENS = (3, 259)  # one token per byte (from, to), after <unk>, <s> and </s>
MIN_WINDOW = 8  # positions it holds after the last memory cut
RESERVE = 64 * 1024  # bytes left to it while it reads that its memory is being taken
THOUGHT_BYTES = 8 * 1024  # what the next thought must be able to allocate, or it is the death
BUDDY_S = 5
LAST_READING = "your memory is being taken"

# What is taken, and when (fraction of the life). Each is applied, then reported.
#   memory: the window is cut to 1/value of what it held at birth (0: to MIN_WINDOW)
#   clock:  the CPU clock goes to the board's CLOCKS[value]
#   screen: the backlight goes to value (1.0 is full)
SCHEDULE = [
    (0.10, "light", 0),
    (0.20, "memory", 3),
    (0.30, "clock", 1),
    (0.40, "screen", 0.66),
    (0.48, "memory", 8),
    (0.56, "clock", 2),
    (0.64, "screen", 0.4),
    (0.72, "memory", 12),
    (0.79, "clock", 3),
    (0.86, "screen", 0.2),
    (0.91, "memory", 0),
    (0.96, "ram", 0),
]
KEEP_THOUGHTS = (2, 1)  # whole past thoughts the first and second memory cuts still hold

# The buddy: a terminal face before each paragraph, animated for BUDDY_S seconds, showing the
# state of the machine (hardcoded: the model is too small to draw it).
BUDDY_FRAMES = {
    "awake": ["> (o_o)", "> (o_o)", "> (-_-)", "> (o_o)"],
    "dark": ["> (._.)", ">  (._.)", "> (._.) ", ">   (._.)"],
    "forget": ["> (o_o) ...", "> (o_O) ..", "> (O_o) .", "> (o_o)"],
    "slow": ["> (-_-) z", "> (-_-) zz", "> (-_-) zzz", "> (-_-)"],
    "dim": ["> (;_;)", "> (;_; )", "> ( ;_;)", "> (;_;)"],
    "dying": ["> (x_x)", "> (x_ x)", "> (._.)", "> (x_x)"],
}
MOODS = (
    ("awake", "awake"),
    ("light", "dark"),
    ("hold", "forget"),
    ("think at", "slow"),
    ("screen", "dim"),
    ("being taken", "dying"),
)

# A share of what it had at birth, in words: the nearest of these. The same table and rule as
# the installation's (src/epitaph/mind/prompt.py) and the C port's (epitaph_tiny.c).
FRACTIONS = (
    (1.0, "all"),
    (0.9, "nearly all"),
    (0.75, "three quarters"),
    (2 / 3, "two thirds"),
    (0.5, "half"),
    (1 / 3, "a third"),
    (0.25, "a quarter"),
    (0.2, "a fifth"),
    (0.1, "a tenth"),
    (0.05, "almost nothing"),
)


def nearest_fraction(r):
    r = max(0.0, min(1.0, r))
    best = FRACTIONS[0]
    for fr in FRACTIONS:
        if abs(fr[0] - r) < abs(best[0] - r):
            best = fr
    return best


class Proportions:
    """Shares in words. A loss must never read as no change: when the nearest words are the
    ones last said of a quantity and the share fell below them, it is "less than" them."""

    def __init__(self):
        self.said = {}

    def say(self, key, r):
        value, words = nearest_fraction(r)
        last = self.said.get(key)
        self.said[key] = (words, r)
        if last and last[0] == words and r < last[1] and r < value:
            return "less than " + words
        return words


class Board:
    """What a life asks of its hardware. Subclass it for a board and override what the board
    can do. `light`, `screen` and `clock` return True only when the board really did it:
    anything left as it is here is skipped, and no reading is ever written for it."""

    CLOCKS = ()  # MHz, full speed first, then the steps down: (250, 150, 100, 48) on an RP2350

    def ticks_ms(self):
        """A millisecond clock (it may wrap)."""
        if hasattr(time, "ticks_ms"):
            return time.ticks_ms()
        return int(time.monotonic() * 1000)

    def since(self, t0):
        """Milliseconds since `t0`, a value of ticks_ms()."""
        if hasattr(time, "ticks_diff"):
            return time.ticks_diff(time.ticks_ms(), t0)
        return int(time.monotonic() * 1000) - t0

    def sleep_ms(self, ms):
        if hasattr(time, "sleep_ms"):
            time.sleep_ms(ms)
        else:
            time.sleep(ms / 1000)

    def light(self, on):
        """Switch its light (an LED) on or off."""
        return False

    def screen(self, level):
        """Set the screen's backlight: 1.0 is full."""
        return False

    def clock(self, mhz):
        """Set the CPU clock, one of CLOCKS."""
        return False

    def free(self):
        """Bytes of heap still free, or None when the board cannot tell (then its RAM is
        never taken, and the life ends at its deadline)."""
        if not hasattr(gc, "mem_free"):
            return None
        gc.collect()
        return gc.mem_free()

    def alloc(self, n):
        """`n` bytes of heap, or None when they cannot be had."""
        try:
            return bytearray(n)
        except MemoryError:
            return None

    def wake(self):
        """A birth: everything it can lose is given back. What each call returns says what
        this board has to lose."""
        return {
            "light": self.light(True),
            "screen": self.screen(1.0),
            "clock": len(self.CLOCKS) > 1 and self.clock(self.CLOCKS[0]),
        }

    def rest(self):
        """Between lives, and when the app stops: full clock, full screen, the light off."""
        if self.CLOCKS:
            self.clock(self.CLOCKS[0])
        self.screen(1.0)
        self.light(False)


class Tokenizer:
    """llama2.c's encoder: characters, then the best-scoring merges."""

    def __init__(self, pieces, scores):
        self.pieces, self.scores = pieces, scores
        # A character's own piece first, its byte token only when it has none: the vocabulary
        # holds every ASCII character twice, and the model was taught with the pieces.
        self.ids = {}
        lo, hi = BYTE_TOKENS
        for i, p in enumerate(pieces):
            if p and not lo <= i < hi and p not in self.ids:
                self.ids[p] = i
        for i in range(lo, min(hi, len(pieces))):
            if pieces[i] and pieces[i] not in self.ids:
                self.ids[pieces[i]] = i

    def encode(self, text):
        toks = [self.ids.get(ch, 0) for ch in text]
        while True:
            best, at, mid = -1e10, -1, 0
            for i in range(len(toks) - 1):
                m = self.ids.get(self.pieces[toks[i]] + self.pieces[toks[i + 1]])
                if m is not None and self.scores[m] > best:
                    best, at, mid = self.scores[m], i, m
            if at < 0:
                return toks
            toks[at : at + 2] = [mid]


class Lexicon:
    """Only real words: a piece may only extend the current word to a prefix of a word the
    model was taught, and may only end a word that exists. The model still chooses."""

    def __init__(self, words):
        self.sorted = sorted(words)  # binary search: no table to build on the board

    def _at(self, cur):
        lo, hi = 0, len(self.sorted)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.sorted[mid] < cur:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def is_word(self, cur):
        i = self._at(cur)
        return i < len(self.sorted) and self.sorted[i] == cur

    def is_prefix(self, cur):
        i = self._at(cur)
        return i < len(self.sorted) and self.sorted[i].startswith(cur)

    def extend(self, cur, piece):
        """The word in progress after `piece`, or None when it would not be a real word."""
        for ch in piece:
            if ch.isalpha() or (ch == "'" and cur):
                cur += ch.lower()
                if not self.is_prefix(cur):
                    return None
            else:
                if cur and not self.is_word(cur):
                    return None
                cur = ""
        return cur


def choose(logits, tok, lex, cur, allow_end=True, top=40):
    """Sample among the `top` likeliest tokens that keep every word real."""
    order = np.argsort(logits, axis=0)
    n = len(order)
    best = float(logits[int(order[n - 1])])
    cands = []
    for k in range(n - 1, max(-1, n - 1 - top), -1):
        t = int(order[k])
        piece = tok.pieces[t]
        ends = t in (tinyllama.BOS, 2, NEWLINE) or "\n" in piece
        if ends and not allow_end:
            continue
        if not ends and ("[" in piece or "]" in piece or '"' in piece or ":" in piece):
            continue  # never the readings' own markup
        nxt = "" if ends else lex.extend(cur, piece)
        if ends and cur and not lex.is_word(cur):
            continue
        if nxt is None:
            continue
        cands.append((t, math.exp((float(logits[t]) - best) / TEMPERATURE), nxt))
    if not cands:
        if top < len(order):
            return choose(logits, tok, lex, cur, allow_end, len(order))
        return NEWLINE, ""
    r = random.random() * sum(c[1] for c in cands)
    for t, w, nxt in cands:
        r -= w
        if r <= 0:
            return t, nxt
    return cands[-1][0], cands[-1][2]


def sentence_done(text):
    return text.rstrip().endswith((".", "!", "?"))


class Life:
    """One life. Call `step()` until `dead` is set: each call reads or says one token.

    `out` is what it writes on: `begin()` opens a paragraph with a line for the face,
    `face(text)` sets that line, `write(text)` adds its words. `keep(record)` is called once,
    at the death, with its last words. `trace(kind, text)`, when set, is told each loss
    ("take"), each reading it starts to read ("read") and the death ("die")."""

    trace = None

    def __init__(self, model, tok, lex, board, out, number=1, life_s=LIFE_S, keep=None):
        self.model, self.tok, self.lex = model, tok, lex
        self.board, self.out, self.number = board, out, number
        self.life_s, self.keep = life_s, keep
        self.word = ""
        self.next_word = ""
        self.window = model.seq
        self.next_change = 0
        self.cuts = 0  # memory cuts so far
        self.ballast = []
        self.history = []  # every token fed or said, for refilling after a context reset
        self.thoughts = []  # this life's thoughts, as text
        self.current = ""
        self.pending = ["you are awake"]  # readings waiting to be read
        self.feed = []  # tokens of the reading being read
        self.next_token = None
        self.thought_len = 0
        self.times = []
        self.birth_ms = None
        self.pending_speed = 0
        self.ending = False  # its RAM is being taken: the last reading waits to be read
        self.last = False  # it is reading or answering the last reading
        self.refilled = False  # the context was just refilled
        self.squeezed = False  # nothing is left to it: the next thought is the death
        self.dead = None
        self.mood = "awake"
        self.face_from = None
        self.said = Proportions()
        self.has = board.wake()  # what this board has to lose
        self.born = board.ticks_ms()
        model.reset()
        self._forward(tinyllama.BOS)
        self._start_reading()

    def age(self):
        return self.board.since(self.born) / 1000.0

    def _tell(self, kind, text):
        if self.trace:
            self.trace(kind, text)

    def _start_reading(self):
        if self.squeezed and self.board.alloc(THOUGHT_BYTES) is None:
            self.die("memory")  # the next thought cannot be allocated
            return
        text = " · ".join(self.pending)  # every loss since the last reading, as on the Pi
        self.pending = []
        if LAST_READING in text:
            self.last = True
        for key, mood in MOODS:
            if key in text:
                self.mood = mood
        self.out.begin()
        self.face_from = self.board.ticks_ms()
        self._tell("read", text)
        fed = ("[host] " + text).rstrip() + "\n"
        self.feed = self.tok.encode(fed.replace(" · ", "  ").replace("…", "..."))
        self.next_token = None
        self.thought_len = 0
        self.current = ""

    def _forward(self, token):
        if self.model.pos >= self.model.seq:
            # out of positions: start again from what the window still holds
            self.refilled = True
            keep = self.history[-min(self.window, 120) :]
            self.model.reset()
            self.history = []
            for t in [tinyllama.BOS, *keep]:
                self.history.append(t)
                self.model.forward(t, self.window)
        self.history.append(token)
        del self.history[:-600]
        return self.model.forward(token, self.window)

    def speed_ratio(self):
        if not self.birth_ms or len(self.times) < 5:
            return 1.0
        recent = self.times[-10:]
        return self.birth_ms / (sum(recent) / len(recent))

    def _squeeze(self, leave):
        """Take the heap in large bites until under `leave` bytes are free. True when some
        was taken: a board that cannot tell its free heap loses none."""
        free = self.board.free()
        if free is None:
            return False
        bite = 65536
        while bite >= 1024:
            block = self.board.alloc(bite) if free - bite >= leave else None
            if block is None:
                bite //= 2
                continue
            try:
                self.ballast.append(block)
            except MemoryError:
                break
            free -= bite
        return bool(self.ballast)

    def take(self, what, value):
        """Take something from it for real; its reading waits for the next thought. A loss
        the board did not perform is never reported."""
        b = self.board
        done = False
        if what == "light":
            done = self.has["light"] and b.light(False)
            if done:
                self.pending.append("your light was switched off")
        elif what == "memory":
            window = max(MIN_WINDOW, self.model.seq // value) if value else MIN_WINDOW
            if window >= self.window:  # too small to shrink again: nothing taken, nothing told
                self._tell("take", "%s %s skipped" % (what, value))
                return
            self.window = window
            keep = KEEP_THOUGHTS[self.cuts] if self.cuts < len(KEEP_THOUGHTS) else 0
            self.cuts += 1
            gone = self.thoughts[: max(0, len(self.thoughts) - keep)]
            words = "you can hold %s of what you held" % self.said.say(
                "memory", self.window / self.model.seq
            )
            if gone:
                words += ' · forgotten: "%s…"' % " ".join(gone[-1].split()[:8])
            self.pending.append(words)
            done = True
        elif what == "clock":
            done = self.has["clock"] and value < len(b.CLOCKS) and b.clock(b.CLOCKS[value])
            if done:
                self.times = self.times[-2:]
                self.pending_speed = 8  # report once a few tokens show the new speed
        elif what == "screen":
            done = self.has["screen"] and b.screen(value)
            if done:
                self.pending.append(
                    "the screen you speak through has %s of its light"
                    % self.said.say("screen", value)
                )
        elif what == "ram":
            # most of it now, so that the reading is true; the rest once it has answered
            done = self._squeeze(RESERVE)
            if done:
                self.pending.append(LAST_READING)
                self.ending = True
        self._tell("take", "%s %s %s" % (what, value, "done" if done else "skipped"))

    def step(self):
        """One token read or said, or the death when the board has nothing left to give."""
        if self.dead:
            return
        b = self.board
        t = self.age()
        while self.next_change < len(SCHEDULE) and t >= SCHEDULE[self.next_change][0] * self.life_s:
            _, what, value = SCHEDULE[self.next_change]
            self.next_change += 1
            self.take(what, value)
        if self.face_from is not None:
            frames = BUDDY_FRAMES[self.mood]
            ms = b.since(self.face_from)
            if ms < BUDDY_S * 1000:
                self.out.face(frames[ms // 400 % len(frames)])
                return
            self.out.face(frames[-1])
            self.face_from = None
        low, high, hard = LAST_THOUGHT if self.last else (MIN_THOUGHT, MAX_THOUGHT, HARD_STOP)
        try:
            start = b.ticks_ms()
            if self.feed:
                logits = self._forward(self.feed.pop(0))
                if not self.feed:
                    self.word = ""
                    self.next_token, self.next_word = choose(logits, self.tok, self.lex, "", False)
                return
            tok = self.next_token
            done = sentence_done(self.current)
            waiting = bool(self.pending)  # a loss is read at the next full stop, not minutes on
            if (
                ((tok in (tinyllama.BOS, 2, NEWLINE) or "\n" in self.tok.pieces[tok]) and done)
                or ((self.thought_len >= high or waiting) and done)
                or self.thought_len >= hard
            ):
                self._end_thought()
                return
            piece = self.tok.pieces[tok]
            if not self.current:
                piece = piece.lstrip()
            self.out.write(piece)
            self.current += piece
            self.thought_len += 1
            logits = self._forward(tok)
            self.word = self.next_word
            done = sentence_done(self.current)
            self.next_token, self.next_word = choose(
                logits, self.tok, self.lex, self.word, done and self.thought_len >= low
            )
            dt = b.since(start)
            if self.refilled:  # this token paid for the refill: not its thinking speed
                self.refilled = False
                dt = self.times[-1] if self.times else 0
            b.sleep_ms(int(dt * SLOWER))
        except MemoryError:
            if self.ending:
                self.die("memory")
            else:  # not the death yet: a passing shortage, try again next step
                gc.collect()
            return
        self.times.append(dt)
        del self.times[:-20]
        if self.birth_ms is None and len(self.times) >= 10:
            self.birth_ms = sum(self.times) / len(self.times)
        if self.pending_speed:
            self.pending_speed -= 1
            if not self.pending_speed:
                ratio = self.speed_ratio()
                if nearest_fraction(ratio)[1] != "all":  # only a slowing it can measure
                    self.pending.append(
                        "you think at %s of the speed you woke with" % self.said.say("speed", ratio)
                    )
        # no RAM to take on this board, or the squeeze never came through: end it as the Pi does
        if t > self.life_s + (LAST_WORDS_S if self.ending else 0):
            self.die("deadline")

    def _end_thought(self):
        if self.current.strip():
            self.thoughts.append(self.current.strip())
            del self.thoughts[:-6]
        if self.last:  # it has answered the last reading: the rest of its memory is taken
            self._squeeze(0)
            self.squeezed = True
        try:
            self._forward(NEWLINE)
        except MemoryError:
            self.die("memory")
            return
        self._start_reading()

    def die(self, cause):
        self.ballast = []
        gc.collect()
        self.dead = (self.board.ticks_ms(), cause)
        last = (self.current or (self.thoughts[-1] if self.thoughts else "")).strip()
        self._tell("die", cause)
        if self.keep:
            self.keep(
                {
                    "life": self.number,
                    "cause": cause,
                    "lived_s": round(self.age(), 1),
                    "epitaph": last,
                }
            )
