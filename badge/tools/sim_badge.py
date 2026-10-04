"""Run the Tufty app on a laptop: stubs for the badge, numpy for ulab, a virtual clock and a
heap the badge can run out of. Each token costs the badge's measured 125 ms, scaled by its
CPU clock.

    python badge/tools/sim_badge.py [minutes] [--check]

`--check` (what `make badge` runs) stops at the first death and fails unless the life went as
it must: every loss performed and read, no reading said twice, the last reading read and
answered, and a death by memory.
"""

import gc
import sys
import tempfile
import types
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "tufty" / "epitaph"
ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
CHECK = "--check" in sys.argv
MINUTES = float(ARGS[0]) if ARGS else 16
HEAP = 7 * 1024 * 1024  # the badge's free heap beside the model (8 MB of PSRAM)
clock = {"ms": 0, "mhz": 250}
heap = {"free": HEAP}
seen = {"take": [], "read": [], "die": [], "lights": [], "screen": []}

fake_time = types.ModuleType("time")
fake_time.ticks_ms = lambda: int(clock["ms"])
fake_time.ticks_diff = lambda a, b: a - b
fake_time.ticks_add = lambda a, b: a + b
fake_time.sleep_ms = lambda ms: clock.__setitem__("ms", clock["ms"] + ms)
sys.modules["time"] = fake_time
gc.mem_free = lambda: heap["free"]


class Heap(bytearray):
    """A bytearray charged to the simulated heap: MemoryError when it is spent."""

    def __new__(cls, n):
        if n > heap["free"]:
            raise MemoryError
        heap["free"] -= n
        return super().__new__(cls)  # the bytes themselves are not needed

    def __init__(self, n):
        super().__init__()
        self.n = n

    def __del__(self):
        heap["free"] += self.n


machine = types.ModuleType("machine")
machine.freq = lambda hz=None: (
    clock.__setitem__("mhz", hz // 1000000) if hz else clock["mhz"] * 1000000
)
sys.modules["machine"] = machine
bw = types.ModuleType("badgeware")
bw.set_brightness = lambda v: seen["screen"].append(v)
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
        seen["lights"].append(v)


sys.path.insert(0, str(APP))
import life
import tinyllama

real_forward = tinyllama.Model.forward


def forward(self, token, window=0):
    clock["ms"] += 125 * 250 / clock["mhz"]
    return real_forward(self, token, window)


def trace(kind, text):
    seen[kind].append(text)
    print(f"  {clock['ms'] / 1000:7.1f}s {kind} {text}")


tinyllama.Model.forward = forward
life.Life.trace = staticmethod(trace)


def run(update):
    frames = 0
    while clock["ms"] < MINUTES * 60000 and not (CHECK and seen["die"]):
        update()
        clock["ms"] += 1  # the frame itself
        frames += 1
    print("ran", frames, "frames to", clock["ms"] / 1000, "s")


def check():
    took = [t.split()[0] for t in seen["take"] if t.endswith("done")]
    assert len(took) == len(life.SCHEDULE), seen["take"]  # the badge can take all of it
    read = [r for r in seen["read"] if r]
    for what in ("your light", "you can hold", "you think at", "the screen", life.LAST_READING):
        assert any(what in r for r in read), "never read: " + what
    plain = [part for r in read for part in r.split(" · ") if not part.startswith("forgotten")]
    assert len(set(plain)) == len(plain), f"a reading said twice: {plain}"
    assert seen["read"][-1].endswith(life.LAST_READING), "it did not die on its last answer"
    assert seen["die"] == ["memory"], seen["die"]
    assert seen["lights"][-1] == 0.0 and seen["screen"][-1] in (0.2, 1.0)
    assert heap["free"] == HEAP, "the heap was not given back at the death"
    assert life.LIFE_S < clock["ms"] / 1000 < life.LIFE_S + life.LAST_WORDS_S
    print("check: every loss read once, the last reading answered, a death by memory")


TMP = tempfile.mkdtemp(prefix="epitaph-badge-")
src = (APP / "__init__.py").read_text()
for name in ("STATE", "LIVES", "ERRORS"):
    assert f'{name} = "/' in src
    src = src.replace(f'{name} = "/', f'{name} = "{TMP}/')
src = src.replace('APP = "/system/apps/epitaph"', f'APP = "{APP}"')
life.Board.alloc = lambda self, n: _alloc(n)


def _alloc(n):
    try:
        return Heap(n)
    except MemoryError:
        return None


g = {
    "badge": Badge(),
    "screen": Screen(),
    "HIRES": 1,
    "VSYNC": 2,
    "color": types.SimpleNamespace(rgb=lambda *a: Color()),
    "font": types.SimpleNamespace(smart=None),
    "run": run,
    "__name__": "app",
}
exec(compile(src, "app", "exec"), g)
if CHECK:
    check()
