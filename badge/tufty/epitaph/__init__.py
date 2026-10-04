# epitaph, the badge edition: a 260K-parameter model taught epitaph's voice (Qwen3 4B's
# thoughts under the installation's own prompt) lives on this badge for ten minutes while
# the badge takes its world away, then dies of memory and is born again.
#
# The life itself is in life.py, and knows no board. This file is the Tufty 2350: what the
# badge can take (`Tufty`), the page its words are drawn on, and the app's frame loop.

badge.mode(HIRES | VSYNC)  # before the imports: mode() rebinds the screen

import gc
import json
import sys
import time

import machine

APP = "/system/apps/epitaph"
sys.path.insert(0, APP)

import life
import tinyllama

try:
    import badgeware
except ImportError:
    badgeware = None

SILENCE_S = 30  # dark between lives
SCALE = 2  # text size on screen

STATE = "/epitaph_state.json"
LIVES = "/epitaph_lives.jsonl"
ERRORS = "/epitaph_errors.log"
KEEP_LIVES = 50

INK = color.rgb(235, 235, 225)
BUDDY = color.rgb(217, 119, 87)  # the buddy's terminal amber
DARK = color.rgb(8, 8, 10)


class Tufty(life.Board):
    """What the badge can take: its rear lights, its backlight, its CPU clock and its heap.
    Each returns True only when the badge did it."""

    CLOCKS = (250, 150, 100, 48)

    def light(self, on):
        try:
            badge.caselights(0.25 if on else 0.0)
        except Exception:
            return False
        return True

    def screen(self, level):
        try:
            badgeware.set_brightness(level)
        except Exception:
            return False
        return True

    def clock(self, mhz):
        try:
            machine.freq(mhz * 1000000)
        except Exception:
            return False
        return machine.freq() == mhz * 1000000


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


def log_error(e, age):
    try:
        import io

        buf = io.StringIO()
        sys.print_exception(e, buf)
        with open(ERRORS, "a") as f:
            f.write("t=%.1f mem=%d\n%s\n" % (age, gc.mem_free(), buf.getvalue()))
    except Exception:
        pass


class Page:
    """Word-wrapped lines, newest at the bottom; each line has a colour."""

    def __init__(self, width, font_):
        self.width = width
        self.font = font_
        self.lines = [["", INK]]
        self.buddy = None  # the line the buddy is animating
        screen.font = font_
        self.line_h = screen.measure_text("Ag")[1] * SCALE + 3

    def _fits(self, s):
        return screen.measure_text(s)[0] * SCALE <= self.width

    def newline(self, pen=INK):
        if self.lines[-1][0] == "":
            self.lines[-1][1] = pen
        else:
            self.lines.append(["", pen])
        del self.lines[:-40]

    def begin(self):
        """A new paragraph: the buddy's line, then the line its words start on."""
        self.newline(BUDDY)
        self.write("> ", BUDDY)
        self.buddy = self.lines[-1]
        self.newline(INK)

    def face(self, text):
        if self.buddy is not None:
            self.buddy[0] = text

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

    def draw(self, x, y_bottom, fade=1.0):
        screen.font = self.font
        y = y_bottom - self.line_h
        for text, pen in reversed(self.lines):
            if y < -self.line_h:
                break
            if fade < 1.0:
                pen = pen.with_alpha(int(255 * fade))
            screen.pen = pen
            screen.text(text, x, y, SCALE)
            y -= self.line_h


def main():
    screen.pen = DARK
    screen.clear()
    screen.font = font.smart
    screen.pen = BUDDY
    screen.text("> (-_-) waking", 8, 8, SCALE)
    badge.update()
    tok = life.Tokenizer(
        load_json(APP + "/assets/vocab.json", []), load_json(APP + "/assets/scores.json", [])
    )
    model = tinyllama.Model(APP + "/assets/model.bin")
    lex = life.Lexicon(load_json(APP + "/assets/lexicon.json", []))
    board = Tufty()
    state = load_json(STATE, {"life": 0})
    cur = [None, None]  # the life and its page
    silence_from = [None]

    def begin():
        state["life"] = state.get("life", 0) + 1
        save_state(state)
        gc.collect()
        page = Page(screen.width - 16, font.smart)
        cur[0] = life.Life(model, tok, lex, board, page, state["life"], keep=keep_epitaph)
        cur[1] = page

    begin()

    def update():
        screen.pen = DARK
        screen.clear()
        alive, page = cur
        if alive is None:
            if time.ticks_diff(time.ticks_ms(), silence_from[0]) >= SILENCE_S * 1000:
                begin()
            return None
        if alive.dead is None:
            try:
                alive.step()
            except Exception as e:  # log it and keep living; the log says why
                log_error(e, alive.age())
            page.draw(8, screen.height - 6)
            return None
        since = time.ticks_diff(time.ticks_ms(), alive.dead[0]) / 1000.0
        if since < 6:
            page.draw(8, screen.height - 6)
        elif since < 10:
            page.draw(8, screen.height - 6, 1.0 - (since - 6) / 4)
        else:
            board.rest()
            cur[0] = cur[1] = None
            silence_from[0] = time.ticks_ms()
        return None

    try:
        run(update)
    finally:
        board.rest()


main()
