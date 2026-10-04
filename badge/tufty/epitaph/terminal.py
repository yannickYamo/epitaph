# epitaph in a terminal: the smallest port. It runs on any board with MicroPython and ulab
# (its words go to the serial port), and on a laptop with Python and numpy:
#
#     python badge/tufty/epitaph/terminal.py [life seconds] [lives]
#
# Copy this folder to the board and run this file. To give the life more to lose, fill in
# `Terminal` below for your board: the LED pin, the clock steps, a backlight. Whatever is left
# out is skipped: on a laptop nothing can be taken but its memory window, and the life ends
# at its deadline instead of a death by RAM.

import json
import sys

try:
    HERE = __file__.rsplit("/", 1)[0] if "/" in __file__ else "."
except NameError:  # run without a file name
    HERE = "."
sys.path.insert(0, HERE)

import life
import tinyllama

try:
    import machine
except ImportError:  # a laptop
    machine = None


class Terminal(life.Board):
    """A bare board: its LED, and its CPU clock when CLOCKS names the steps it can run at."""

    # MHz, full speed first: (250, 150, 100, 48) on an RP2350, (240, 160, 80) on an ESP32
    CLOCKS = ()
    LED = "LED"  # the pin of the on-board LED

    def __init__(self):
        self.led = None
        if machine is not None:
            try:
                self.led = machine.Pin(self.LED, machine.Pin.OUT)
            except Exception:
                pass  # no LED on that pin: its light is never taken

    def light(self, on):
        if self.led is None:
            return False
        self.led.value(1 if on else 0)
        return self.led.value() == (1 if on else 0)

    def clock(self, mhz):
        if machine is None:
            return False
        try:
            machine.freq(mhz * 1000000)
        except Exception:
            return False
        return machine.freq() == mhz * 1000000


class Out:
    """Words on the serial port; the face is redrawn in place before each paragraph."""

    def begin(self):
        sys.stdout.write("\n\n")
        self.shown = None

    def face(self, text):
        if text != self.shown:
            sys.stdout.write("\r%-16s" % text)
            self.shown = text

    def write(self, text):
        if self.shown is not None:
            sys.stdout.write("\n")
            self.shown = None
        sys.stdout.write(text)


def load(name):
    with open(HERE + "/assets/" + name) as f:
        return json.load(f)


def main(life_s=life.LIFE_S, lives=0, silence_s=30):
    """Live `lives` lives (0: for ever), `silence_s` of darkness between two."""
    board, out = Terminal(), Out()
    tok = life.Tokenizer(load("vocab.json"), load("scores.json"))
    lex = life.Lexicon(load("lexicon.json"))
    model = tinyllama.Model(HERE + "/assets/model.bin")
    number = 0
    try:
        while not lives or number < lives:
            number += 1
            cur = life.Life(model, tok, lex, board, out, number, life_s)
            while cur.dead is None:
                cur.step()
                if hasattr(sys.stdout, "flush"):
                    sys.stdout.flush()
            sys.stdout.write("\n\n[life %d: %s after %d s]\n" % (number, cur.dead[1], cur.age()))
            board.rest()
            if not lives or number < lives:
                board.sleep_ms(silence_s * 1000)
    finally:
        board.rest()


if __name__ == "__main__":
    args = [float(a) for a in getattr(sys, "argv", [])[1:3]]
    main(args[0] if args else life.LIFE_S, int(args[1]) if len(args) > 1 else 0)
