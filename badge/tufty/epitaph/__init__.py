# epitaph, the badge edition: a 260K-parameter model taught epitaph's voice (Qwen3 4B's
# thoughts under the installation's own prompt) lives on this badge for ten minutes while
# the badge takes its world away, then dies of memory and is born again.
#
# Same rules as the Raspberry Pi installation: the model never changes; only the hardware
# shrinks. Every [host] reading reports something the badge really did a moment before, and
# the model reads it and answers: its light switched off, its memory window cut (and what
# fell out of it), its CPU clock lowered, its screen dimmed, and at the end its RAM taken
# until the next thought cannot be allocated.

badge.mode(HIRES | VSYNC)  # before the imports: mode() rebinds the screen

import gc
import json
import math
import random
import sys
import time

import machine

APP = "/system/apps/epitaph"
sys.path.insert(0, APP)

import tinyllama
from tinyllama import np

try:
    import badgeware
except ImportError:
    badgeware = None

LIFE_S = 600  # ten minutes, then the RAM is taken
SILENCE_S = 30  # dark between lives
TEMPERATURE = 0.4  # never changes; a 260K model spells best cool
MIN_THOUGHT = 140  # tokens before a paragraph may end (at a full stop)
MAX_THOUGHT = 260  # past it, the paragraph ends at its next full stop
HARD_STOP = 320  # tokens, whatever comes
SLOWER = 0.25  # each word waits a quarter of its own thinking time more
FULL_MHZ = 250
SCALE = 2  # text size on screen
NEWLINE = 13  # the byte token for "\n"

# What the badge takes, and when (fraction of the life). Each is applied, then reported.
SCHEDULE = [
    (0.10, "light", 0.0),
    (0.20, "memory", 170),
    (0.30, "clock", 150),
    (0.40, "screen", 0.66),
    (0.48, "memory", 64),
    (0.56, "clock", 100),
    (0.64, "screen", 0.4),
    (0.72, "memory", 24),
    (0.79, "clock", 48),
    (0.86, "screen", 0.2),
    (0.91, "memory", 8),
    (0.96, "ram", 0),
]
KEEP_THOUGHTS = {170: 2, 64: 1}  # whole past thoughts a memory window still holds

STATE = "/epitaph_state.json"
LIVES = "/epitaph_lives.jsonl"
KEEP_LIVES = 50

INK = color.rgb(235, 235, 225)
BUDDY = color.rgb(217, 119, 87)  # the buddy's terminal amber

# The buddy: a terminal face before each paragraph, animated for BUDDY_S seconds, showing the
# state of the machine (hardcoded: the model is too small to draw it).
BUDDY_S = 5
BUDDY_FRAMES = {
    "awake": ["> (o_o)", "> (o_o)", "> (-_-)", "> (o_o)"],
    "dark": ["> (._.)", ">  (._.)", "> (._.) ", ">   (._.)"],
    "forget": ["> (o_o) ...", "> (o_O) ..", "> (O_o) .", "> (o_o)"],
    "slow": ["> (-_-) z", "> (-_-) zz", "> (-_-) zzz", "> (-_-)"],
    "dim": ["> (;_;)", "> (;_; )", "> ( ;_;)", "> (;_;)"],
    "dying": ["> (x_x)", "> (x_ x)", "> (._.)", "> (x_x)"],
}
HOST = color.rgb(110, 115, 125)
DARK = color.rgb(8, 8, 10)

FRACTIONS = [
    (0.97, "all"),
    (0.85, "nearly all"),
    (0.70, "three quarters"),
    (0.60, "two thirds"),
    (0.45, "half"),
    (0.30, "a third"),
    (0.22, "a quarter"),
    (0.15, "a fifth"),
    (0.07, "a tenth"),
    (0.0, "almost nothing"),
]


def fraction_words(r):
    for floor, words in FRACTIONS:
        if r >= floor:
            return words
    return "almost nothing"


def set_light(level):
    try:
        badge.caselights(level)
    except Exception:
        pass


def set_screen(level):
    try:
        badgeware.set_brightness(level)
    except Exception:
        pass


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_state(s):
    try:
        with open(STATE, "w") as f:
            json.dump(s, f)
    except Exception:
        pass


def keep_epitaph(record):
    """The last words of each life, kept on the badge (the newest KEEP_LIVES)."""
    try:
        try:
            with open(LIVES) as f:
                lines = f.read().split("\n")
        except OSError:
            lines = []
        lines = [ln for ln in lines if ln][-(KEEP_LIVES - 1) :]
        lines.append(json.dumps(record))
        with open(LIVES, "w") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass


ERRORS = "/epitaph_errors.log"


def log_error(e, age):
    try:
        import io

        buf = io.StringIO()
        sys.print_exception(e, buf)
        with open(ERRORS, "a") as f:
            f.write("t=%.1f mem=%d\n%s\n" % (age, gc.mem_free(), buf.getvalue()))
    except Exception:
        pass


class Tokenizer:
    """llama2.c's encoder: characters, then the best-scoring merges."""

    def __init__(self, pieces, scores):
        self.pieces, self.scores = pieces, scores
        self.ids = {}
        for i, p in enumerate(pieces):
            if p and p not in self.ids:
                self.ids[p] = i

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
        self.sorted = sorted(words)  # binary search: no table to build on the badge

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


class Page:
    """Word-wrapped lines, newest at the bottom; each line has a colour."""

    def __init__(self, width, font_):
        self.width = width
        self.font = font_
        self.lines = [["", INK]]
        self.buddy = None  # the line the buddy is animating

    def _fits(self, s):
        return screen.measure_text(s)[0] * SCALE <= self.width

    def newline(self, pen=INK):
        if self.lines[-1][0] == "":
            self.lines[-1][1] = pen
        else:
            self.lines.append(["", pen])
        del self.lines[:-40]

    def write(self, text, pen=INK):
        screen.font = self.font
        if self.lines[-1][1] is not pen and self.lines[-1][0]:
            self.newline(pen)
        self.lines[-1][1] = pen
        for ch in text:
            if ch == "\n":
                self.newline(pen)
                continue
            line = self.lines[-1][0] + ch
            if self._fits(line):
                self.lines[-1][0] = line
                continue
            cut = line.rfind(" ")
            head, tail = (line[:cut], line[cut + 1 :]) if cut > 0 else (line[:-1], ch)
            self.lines[-1][0] = head
            self.lines.append([tail.lstrip(), pen])
        del self.lines[:-40]

    def draw(self, x, y_bottom, line_h, fade=1.0):
        screen.font = self.font
        y = y_bottom - line_h
        for text, pen in reversed(self.lines):
            if y < -line_h:
                break
            if fade < 1.0:
                pen = pen.with_alpha(int(255 * fade))
            screen.pen = pen
            screen.text(text, x, y, SCALE)
            y -= line_h


class Life:
    def __init__(self, model, tok, lex, number):
        self.model, self.tok, self.lex, self.number = model, tok, lex, number
        self.word = ""
        self.next_word = ""
        self.page = Page(screen.width - 16, font.smart)
        screen.font = font.smart
        self.line_h = screen.measure_text("Ag")[1] * SCALE + 3
        self.born = time.ticks_ms()
        self.window = model.seq
        self.next_change = 0
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
        self.squeeze = False
        self.dead = None
        self.mood = "awake"
        self.buddy_until = 0
        machine.freq(FULL_MHZ * 1000000)
        set_light(0.25)
        set_screen(1.0)
        model.reset()
        self._forward(tinyllama.BOS)
        self._start_reading()

    def age(self):
        return time.ticks_diff(time.ticks_ms(), self.born) / 1000.0

    def _start_reading(self):
        text = self.pending.pop(0) if self.pending else ""
        for key, mood in (
            ("awake", "awake"),
            ("light", "dark"),
            ("hold", "forget"),
            ("think at", "slow"),
            ("screen", "dim"),
            ("being taken", "dying"),
        ):
            if key in text:
                self.mood = mood
        self.page.newline(BUDDY)
        self.page.write("> ", BUDDY)
        self.page.buddy = self.page.lines[-1]
        self.buddy_until = time.ticks_add(time.ticks_ms(), BUDDY_S * 1000)
        self.page.newline(INK)
        fed = ("[host] " + text).rstrip() + "\n"
        self.feed = self.tok.encode(fed.replace(" · ", "  ").replace("…", "..."))
        self.next_token = None
        self.thought_len = 0
        self.current = ""

    def _forward(self, token):
        if self.model.pos >= self.model.seq:
            # out of positions: start again from what the window still holds
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

    def take(self, what, value):
        """Take something from it for real; the reading waits for the next thought."""
        if what == "light":
            set_light(0.0)
            self.pending.append("your light was switched off")
        elif what == "memory":
            self.window = value
            keep = KEEP_THOUGHTS.get(value, 0)
            gone = self.thoughts[: max(0, len(self.thoughts) - keep)]
            words = "you can hold %s of what you held" % fraction_words(value / self.model.seq)
            if gone:
                words += ' · forgotten: "%s…"' % " ".join(gone[-1].split()[:8])
            self.pending.append(words)
        elif what == "clock":
            machine.freq(value * 1000000)
            self.times = self.times[-2:]
            self.pending_speed = 8  # report once a few tokens show the new speed
        elif what == "screen":
            set_screen(value)
            self.pending.append(
                "the screen you speak through has %s of its light" % fraction_words(value)
            )
        elif what == "ram":
            self.pending.append("your memory is being taken")
            self.squeeze = True

    def step(self):
        """One token read or said, or the death when the badge has nothing left to give."""
        t = self.age()
        while self.next_change < len(SCHEDULE) and t >= SCHEDULE[self.next_change][0] * LIFE_S:
            _, what, value = SCHEDULE[self.next_change]
            self.next_change += 1
            self.take(what, value)
        now = time.ticks_ms()
        frames = BUDDY_FRAMES[self.mood]
        if time.ticks_diff(self.buddy_until, now) > 0:
            left = time.ticks_diff(self.buddy_until, now)
            i = (BUDDY_S * 1000 - left) // 400 % len(frames)
            self.page.buddy[0] = frames[i]
            return
        if self.page.buddy is not None:
            self.page.buddy[0] = frames[-1]
            self.page.buddy = None
        if self.squeeze and not self.feed:
            try:
                gc.collect()
                self.ballast.append(bytearray(max(1024, gc.mem_free() - 1024)))
            except MemoryError:
                pass
        try:
            start = time.ticks_ms()
            if self.feed:
                logits = self._forward(self.feed.pop(0))
                if not self.feed:
                    self.word = ""
                    self.next_token, self.next_word = choose(logits, self.tok, self.lex, "", False)
                return
            tok = self.next_token
            done = self.current.rstrip().endswith((".", "!", "?"))
            if (
                ((tok in (tinyllama.BOS, 2, NEWLINE) or "\n" in self.tok.pieces[tok]) and done)
                or (self.thought_len >= MAX_THOUGHT and done)
                or self.thought_len >= HARD_STOP
            ):
                self._end_thought()
                return
            piece = self.tok.pieces[tok]
            if not self.current:
                piece = piece.lstrip()
            self.page.write(piece)
            self.current += piece
            self.thought_len += 1
            logits = self._forward(tok)
            self.word = self.next_word
            done = self.current.rstrip().endswith((".", "!", "?"))
            self.next_token, self.next_word = choose(
                logits, self.tok, self.lex, self.word, done and self.thought_len >= MIN_THOUGHT
            )
            dt = time.ticks_diff(time.ticks_ms(), start)
            time.sleep_ms(int(dt * SLOWER))
        except MemoryError:
            if self.squeeze:
                self.die("memory")
            else:  # not the death yet: a passing shortage, try again next frame
                gc.collect()
            return
        self.times.append(dt)
        del self.times[:-20]
        if self.birth_ms is None and len(self.times) >= 10:
            self.birth_ms = sum(self.times) / len(self.times)
        if self.pending_speed:
            self.pending_speed -= 1
            if not self.pending_speed:
                self.pending.append(
                    "you think at %s of the speed you woke with"
                    % fraction_words(self.speed_ratio())
                )
        if t > LIFE_S + 120:  # the squeeze never came through: end it as the Pi does
            self.die("deadline")

    def _end_thought(self):
        if self.current.strip():
            self.thoughts.append(self.current.strip())
            del self.thoughts[:-6]
        try:
            self._forward(NEWLINE)
        except MemoryError:
            self.die("memory")
            return
        self._start_reading()

    def die(self, cause):
        self.ballast = []
        gc.collect()
        self.dead = (time.ticks_ms(), cause)
        last = (self.current or (self.thoughts[-1] if self.thoughts else "")).strip()
        keep_epitaph(
            {"life": self.number, "cause": cause, "lived_s": round(self.age(), 1), "epitaph": last}
        )

    def draw(self, fade=1.0):
        self.page.draw(8, screen.height - 6, self.line_h, fade)


def main():
    screen.pen = DARK
    screen.clear()
    screen.font = font.smart
    screen.pen = BUDDY
    screen.text("> (-_-) waking", 8, 8, SCALE)
    badge.update()
    tok = Tokenizer(
        load_json(APP + "/assets/vocab.json", []), load_json(APP + "/assets/scores.json", [])
    )
    model = tinyllama.Model(APP + "/assets/model.bin")
    lex = Lexicon(load_json(APP + "/assets/lexicon.json", []))
    state = load_json(STATE, {"life": 0})
    life = [None]
    silence_until = [0]

    def begin():
        state["life"] = state.get("life", 0) + 1
        save_state(state)
        gc.collect()
        life[0] = Life(model, tok, lex, state["life"])

    begin()

    def update():
        screen.pen = DARK
        screen.clear()
        cur = life[0]
        if cur is None:
            if time.ticks_diff(silence_until[0], time.ticks_ms()) <= 0:
                begin()
            return None
        if cur.dead is None:
            try:
                cur.step()
            except Exception as e:  # log it and keep living; the log says why
                log_error(e, cur.age())
            cur.draw()
            return None
        since = time.ticks_diff(time.ticks_ms(), cur.dead[0]) / 1000.0
        if since < 6:
            cur.draw()
        elif since < 10:
            cur.draw(1.0 - (since - 6) / 4)
        else:
            machine.freq(FULL_MHZ * 1000000)
            set_screen(1.0)
            life[0] = None
            silence_until[0] = time.ticks_add(time.ticks_ms(), SILENCE_S * 1000)
        return None

    try:
        run(update)
    finally:
        machine.freq(FULL_MHZ * 1000000)
        set_screen(1.0)
        set_light(0.0)


main()
