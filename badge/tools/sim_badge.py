"""Run the badge app on a laptop on a virtual clock: stubs for the badge, numpy for ulab.
Each token costs the badge's measured 125 ms, scaled by its CPU clock.

    python badge/tools/sim_badge.py [minutes]
"""

import gc
import sys
import tempfile
import types
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "tufty" / "epitaph"
MINUTES = float(sys.argv[1]) if len(sys.argv) > 1 else 14
clock = {"ms": 0, "mhz": 250}

fake_time = types.ModuleType("time")
fake_time.ticks_ms = lambda: int(clock["ms"])
fake_time.ticks_diff = lambda a, b: a - b
fake_time.ticks_add = lambda a, b: a + b
fake_time.sleep_ms = lambda ms: clock.__setitem__("ms", clock["ms"] + ms)
sys.modules["time"] = fake_time
gc.mem_free = lambda: 4096

machine = types.ModuleType("machine")
machine.freq = lambda hz=None: (
    clock.__setitem__("mhz", hz // 1000000) if hz else clock["mhz"] * 1000000
)
sys.modules["machine"] = machine
bw = types.ModuleType("badgeware")
bw.set_brightness = lambda v: print(f"  {clock['ms'] / 1000:7.1f}s screen {v}")
sys.modules["badgeware"] = bw


class Color:
    def with_alpha(self, a):
        return self


class Screen:
    width, height = 320, 240
    font = pen = None

    def measure_text(self, s, *a):
        return (len(s) * 6, 8)

    def text(self, *a):
        pass

    def clear(self):
        pass


class Badge:
    def mode(self, m):
        pass

    def update(self):
        pass

    def caselights(self, v):
        print(f"  {clock['ms'] / 1000:7.1f}s lights {v}")


g = {
    "badge": Badge(),
    "screen": Screen(),
    "HIRES": 1,
    "VSYNC": 2,
    "color": types.SimpleNamespace(rgb=lambda *a: Color()),
    "font": types.SimpleNamespace(smart=None),
    "__name__": "app",
}

sys.path.insert(0, str(APP))
import tinyllama

real_forward = tinyllama.Model.forward


def forward(self, token, window=0):
    clock["ms"] += 125 * 250 / clock["mhz"]
    return real_forward(self, token, window)


tinyllama.Model.forward = forward


def run(update):
    frames = 0
    while clock["ms"] < MINUTES * 60000:
        update()
        clock["ms"] += 1  # the frame itself
        frames += 1
    print("ran", frames, "frames to", clock["ms"] / 1000, "s")


g["run"] = run
src = (APP / "__init__.py").read_text()
src = src.replace('APP = "/system/apps/epitaph"', f'APP = "{APP}"')
TMP = tempfile.mkdtemp(prefix="epitaph-badge-")
src = src.replace('STATE = "/epitaph_state.json"', f'STATE = "{TMP}/state.json"')
src = src.replace('LIVES = "/epitaph_lives.jsonl"', f'LIVES = "{TMP}/lives.jsonl"')
src = src.replace('ERRORS = "/epitaph_errors.log"', f'ERRORS = "{TMP}/errors.log"')
# log what happens
src = src.replace(
    "    def take(self, what, value):\n",
    "    def take(self, what, value):\n        print('  %7.1fs take %s %s pos=%d' % (self.age(), what, value, self.model.pos))\n",
)
src = src.replace(
    "    def die(self, cause):\n",
    "    def die(self, cause):\n        print('  %7.1fs DIE %s' % (self.age(), cause))\n",
)
src = src.replace(
    "        if self.model.pos >= self.model.seq:\n",
    "        if self.model.pos >= self.model.seq:\n            print('  %7.1fs context reset' % self.age())\n",
)
exec(compile(src, "app", "exec"), g)
