"""face — the face: THE PICTURE. A framebuffer and pure-math animations.

This module is the face's RENDERER, and it lives in the BRAIN (loa/cortex/):
the cortex owns the picture and publishes the 1024 bytes. The panel DRIVER is
loa/oled/driver.py (SH1106 over SPI0: CLK 11 / MOSI 10 / RES 24 / DC 25 / CS 8,
500kHz, pinctrl for DC/RES) and the frame GEOMETRY is loa/geom.py — both are in
a different package on purpose, so the DISPLAY daemon reaches the driver and
the geometry without being able to reach this file: a module holding the driver
and the renderer together is a boundary on paper. Enforced in
tests/test_display_boundary.py.

  - Frame + animations: pure math, no hardware. Draw into a 128x64 page-major
    framebuffer and see it anywhere (NullDisplay, tests, --render). The
    eventual 3.5" face gets its own panel driver and keeps these frames.
  - The frame geometry is read, never re-badged: this module uses FACE_WIDTH /
    FACE_HEIGHT / FACE_PAGES from loa/geom.py and does NOT re-export them, and
    it does not re-export the driver. `face.WIDTH` was a compatibility shim
    left over from the 2026-09-13 split — a renderer answering for the driver
    and the geometry is a name that lies about which layer it is. Callers name
    what they use (geom, oled.driver).

The flatline signature lives here: the default "scope" animation is a flat
line with occasional blips. Because that is what I am.
"""
import math
import random
import time

from . import amiga

from ..fault import __main__ as fault

from . import topaz
from ..geom import FACE_HEIGHT, FACE_PAGES, FACE_WIDTH

# ---------------------------------------------------------------------------
# the state a frame is drawn from — HANDED IN, never fetched
#
# A renderer that reads a file or another service is a renderer that draws
# something other than the state it was given, and the failure is always the
# same shape: a plausible, wrong picture. So this module has no file reads at
# all. The cortex holds the state, reads the body's own files (a sweep, the
# vault), and passes the result in; off-body the caller carries what it got
# off the feed.


def seal_state(st=None) -> dict:
    """The vault's public seal state for a frame, from the caller's state.

    The body's copy is the only copy: /var/lib/vault/status.json exists on the
    loa and nowhere else, and reading it from dixie drew GONE over a vault that
    was sealed and fine. The cortex reads it and passes it in.
    """
    seal = (st or {}).get("frag")
    return seal if isinstance(seal, dict) else {}


def faults_state(st=None) -> dict:
    """The sweep's view for a frame, from the caller's state.

    Same rule: /dev/shm/loa-faults.json has exactly one reader (the cortex's
    ingest) and the renderer is handed the result. The state carries the fault
    ROWS; `fault_ts` is the record that a sweep was heard at all, so a body
    that has never heard one says NO SWEEP instead of ALL QUIET.
    """
    st = st or {}
    f = st.get("faults")
    if isinstance(f, dict):
        return f                       # already a view (a console, a test)
    ts = st.get("fault_ts")
    if ts is None:
        return {}
    rows = [r for r in (f or []) if isinstance(r, dict)]
    return {
        "ts": ts,
        "faults": sum(1 for r in rows if r.get("level") == "fault"),
        "warns": sum(1 for r in rows if r.get("level") == "warn"),
        "rows": rows,
    }


# ---------------------------------------------------------------------------
# Pi 5 rails + under-voltage flags for the PWR page — cached, sanctioned

_POWER_CACHE: dict = {"ts": 0.0, "data": {}}
POWER_TTL_S = 1.0


def power_status() -> dict:
    """Pi 5 rail volts/amps plus the throttled bitfield. Cached ~1s so a
    30fps page can call it every frame; empty dict off-Pi — dixie has no
    rails to report, and must not pretend otherwise.

    ``throttled`` bits: 0 = under-voltage NOW, 2 = throttled NOW,
    16/18 = under-voltage/throttling has happened since boot. Bit 0 is the
    one that matters: it is the board saying its 5V input is sagging, which
    sags 3V3 with it and takes the panel dark.
    """
    now = time.time()
    if _POWER_CACHE["data"] and now - _POWER_CACHE["ts"] < POWER_TTL_S:
        return _POWER_CACHE["data"]
    data: dict = {}
    try:
        import subprocess
        r = subprocess.run(["vcgencmd", "pmic_read_adc"],
                           capture_output=True, text=True, timeout=3)
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                if "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.split()[0]        # "3V3_SYS_V volt(9)" -> 3V3_SYS_V
                try:
                    data[key] = float(val.strip().rstrip("VA"))
                except ValueError:
                    continue
        r = subprocess.run(["vcgencmd", "get_throttled"],
                           capture_output=True, text=True, timeout=3)
        if r.returncode == 0 and "=" in r.stdout:
            data["throttled"] = int(r.stdout.strip().split("=", 1)[1], 16)
    except Exception:
        pass
    _POWER_CACHE["ts"] = now
    _POWER_CACHE["data"] = data
    return data

# ---------------------------------------------------------------------------
# framebuffer

class Frame:
    """128x64 page-major framebuffer (SH1106 layout: page 0..7, LSB = top)."""

    def __init__(self):
        self.buf = bytearray(FACE_PAGES * FACE_WIDTH)

    def clear(self):
        self.buf = bytearray(FACE_PAGES * FACE_WIDTH)

    def px(self, x, y, on=True):
        if not (0 <= x < FACE_WIDTH and 0 <= y < FACE_HEIGHT):
            return
        i = (y >> 3) * FACE_WIDTH + x
        bit = 1 << (y & 7)
        if on:
            self.buf[i] |= bit
        else:
            self.buf[i] &= ~bit

    def line(self, x0, y0, x1, y1, on=True):
        """Bresenham. Draws into the buffer; no display call."""
        dx = abs(x1 - x0)
        sx = 1 if x0 < x1 else -1
        dy = -abs(y1 - y0)
        sy = 1 if y0 < y1 else -1
        err = dx + dy
        while True:
            self.px(x0, y0, on)
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy

    def text(self, x, y, s, on=True):
        """5x7 bitmap text, uppercase. Unknown chars render as spaces."""
        s = s.upper()
        for ch in s:
            glyph = FONT.get(ch, FONT[" "])
            for cx in range(5):
                col = glyph[cx]
                for cy in range(7):
                    if col & (1 << cy):
                        self.px(x + cx, y + cy, on)
            x += 6

    def text_width(self, s):
        return len(s) * 6

    def text3x5(self, x, y, s, on=True):
        """3x5 bitmap text, uppercase, 4px advance — status-board density."""
        s = s.upper()
        for ch in s:
            glyph = FONT3X5.get(ch, FONT3X5[" "])
            for cx in range(3):
                col = glyph[cx]
                for cy in range(5):
                    if col & (1 << cy):
                        self.px(x + cx, y + cy, on)
            x += 4

    def text3x5_width(self, s):
        return len(s) * 4

    def blit(self, display, offset=None):
        display.show(self.buf, offset)


# ---------------------------------------------------------------------------
# 5x7 font (columns, bit 0 = top row). Lowercase maps to uppercase.

FONT = {
    " ": (0x00, 0x00, 0x00, 0x00, 0x00),
    "!": (0x04, 0x04, 0x04, 0x00, 0x04),
    '"': (0x0A, 0x0A, 0x00, 0x00, 0x00),
    "#": (0x0A, 0x1F, 0x0A, 0x1F, 0x0A),
    "$": (0x0E, 0x15, 0x0E, 0x15, 0x0E),
    "%": (0x12, 0x15, 0x0A, 0x15, 0x09),
    "&": (0x0C, 0x12, 0x0C, 0x12, 0x0D),
    "'": (0x04, 0x04, 0x00, 0x00, 0x00),
    "(": (0x04, 0x08, 0x08, 0x08, 0x04),
    ")": (0x04, 0x02, 0x02, 0x02, 0x04),
    "*": (0x00, 0x0A, 0x04, 0x0A, 0x00),
    "+": (0x00, 0x04, 0x0E, 0x04, 0x00),
    ",": (0x00, 0x00, 0x00, 0x06, 0x04),
    "-": (0x00, 0x00, 0x0E, 0x00, 0x00),
    ".": (0x00, 0x00, 0x00, 0x04, 0x00),
    "/": (0x10, 0x08, 0x04, 0x02, 0x01),
    "0": (0x0E, 0x11, 0x11, 0x11, 0x0E),
    "1": (0x04, 0x0C, 0x04, 0x04, 0x0E),
    "2": (0x0E, 0x11, 0x02, 0x04, 0x1F),
    "3": (0x0E, 0x11, 0x0E, 0x11, 0x0E),
    "4": (0x08, 0x14, 0x0A, 0x1F, 0x08),
    "5": (0x1F, 0x10, 0x1E, 0x01, 0x1E),
    "6": (0x0E, 0x10, 0x1E, 0x11, 0x0E),
    "7": (0x1F, 0x02, 0x04, 0x08, 0x08),
    "8": (0x0E, 0x11, 0x0E, 0x11, 0x0E),
    "9": (0x0E, 0x11, 0x0F, 0x01, 0x0E),
    ":": (0x00, 0x04, 0x00, 0x04, 0x00),
    ";": (0x00, 0x04, 0x00, 0x04, 0x04),
    "<": (0x02, 0x04, 0x08, 0x04, 0x02),
    "=": (0x00, 0x0E, 0x00, 0x0E, 0x00),
    ">": (0x08, 0x04, 0x02, 0x04, 0x08),
    "?": (0x0E, 0x01, 0x02, 0x00, 0x02),
    "@": (0x0E, 0x11, 0x15, 0x11, 0x0E),
    "A": (0x0E, 0x11, 0x1F, 0x11, 0x11),
    "B": (0x1E, 0x11, 0x1E, 0x11, 0x1E),
    "C": (0x0E, 0x11, 0x10, 0x11, 0x0E),
    "D": (0x1C, 0x12, 0x11, 0x12, 0x1C),
    "E": (0x1F, 0x10, 0x1E, 0x10, 0x1F),
    "F": (0x1F, 0x10, 0x1E, 0x10, 0x10),
    "G": (0x0E, 0x11, 0x17, 0x11, 0x0F),
    "H": (0x11, 0x11, 0x1F, 0x11, 0x11),
    "I": (0x0E, 0x04, 0x04, 0x04, 0x0E),
    "J": (0x07, 0x02, 0x02, 0x12, 0x0C),
    "K": (0x11, 0x12, 0x14, 0x12, 0x11),
    "L": (0x10, 0x10, 0x10, 0x10, 0x1F),
    "M": (0x11, 0x1B, 0x15, 0x11, 0x11),
    "N": (0x11, 0x19, 0x15, 0x13, 0x11),
    "O": (0x0E, 0x11, 0x11, 0x11, 0x0E),
    "P": (0x1E, 0x11, 0x1E, 0x10, 0x10),
    "Q": (0x0E, 0x11, 0x15, 0x12, 0x0D),
    "R": (0x1E, 0x11, 0x1E, 0x14, 0x12),
    "S": (0x0F, 0x10, 0x0E, 0x01, 0x1E),
    "T": (0x1F, 0x04, 0x04, 0x04, 0x04),
    "U": (0x11, 0x11, 0x11, 0x11, 0x0E),
    "V": (0x11, 0x11, 0x11, 0x0A, 0x04),
    "W": (0x11, 0x11, 0x15, 0x1B, 0x11),
    "X": (0x11, 0x0A, 0x04, 0x0A, 0x11),
    "Y": (0x11, 0x0A, 0x04, 0x04, 0x04),
    "Z": (0x1F, 0x02, 0x04, 0x08, 0x1F),
    "[": (0x0E, 0x08, 0x08, 0x08, 0x0E),
    "\\": (0x01, 0x02, 0x04, 0x08, 0x10),
    "]": (0x0E, 0x02, 0x02, 0x02, 0x0E),
    "^": (0x04, 0x0A, 0x00, 0x00, 0x00),
    "_": (0x00, 0x00, 0x00, 0x00, 0x1F),
    "`": (0x04, 0x02, 0x00, 0x00, 0x00),
    "|": (0x04, 0x04, 0x04, 0x04, 0x04),
    "~": (0x0A, 0x15, 0x00, 0x00, 0x00),
}

# ---------------------------------------------------------------------------
# 3x5 font — status-board density (rows of 3px, bit set = lit).
# Raw rows: 5 strings of 3 chars; parsed to 3 column bitmasks (bit0 = top).

FONT3X5_RAW = {
    " ": ("000", "000", "000", "000", "000"),
    "!": ("010", "010", "010", "000", "010"),
    ".": ("000", "000", "000", "000", "010"),
    ",": ("000", "000", "000", "010", "100"),
    ":": ("000", "010", "000", "010", "000"),
    "-": ("000", "000", "111", "000", "000"),
    "=": ("000", "111", "000", "111", "000"),
    "/": ("001", "001", "010", "100", "100"),
    "[": ("011", "010", "010", "010", "011"),
    "]": ("110", "010", "010", "010", "110"),
    "0": ("011", "101", "101", "101", "110"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("110", "001", "010", "100", "111"),
    "3": ("110", "001", "110", "001", "110"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "110", "001", "110"),
    "6": ("011", "100", "110", "101", "110"),
    "7": ("111", "001", "010", "010", "010"),
    "8": ("110", "101", "110", "101", "110"),
    "9": ("011", "101", "011", "001", "110"),
    "A": ("010", "101", "111", "101", "101"),
    "B": ("110", "101", "110", "101", "110"),
    "C": ("011", "100", "100", "100", "011"),
    "D": ("110", "101", "101", "101", "110"),
    "E": ("111", "100", "110", "100", "111"),
    "F": ("111", "100", "110", "100", "100"),
    "G": ("011", "100", "101", "101", "011"),
    "H": ("101", "101", "111", "101", "101"),
    "I": ("111", "010", "010", "010", "111"),
    "J": ("001", "001", "001", "101", "010"),
    "K": ("101", "110", "100", "110", "101"),
    "L": ("100", "100", "100", "100", "111"),
    "M": ("101", "111", "111", "101", "101"),
    "N": ("101", "111", "101", "101", "101"),
    "O": ("010", "101", "101", "101", "010"),
    "P": ("110", "101", "110", "100", "100"),
    "Q": ("011", "101", "101", "101", "001"),
    "R": ("110", "101", "110", "101", "101"),
    "S": ("011", "100", "010", "001", "110"),
    "T": ("111", "010", "010", "010", "010"),
    "U": ("101", "101", "101", "101", "010"),
    "V": ("101", "101", "101", "010", "010"),
    "W": ("101", "101", "111", "111", "101"),
    "X": ("101", "010", "010", "010", "101"),
    "Y": ("101", "101", "010", "010", "010"),
    "Z": ("111", "001", "010", "100", "111"),
}

FONT3X5 = {
    ch: tuple(
        sum((1 << r) for r in range(5) if rows[r][c] == "1")
        for c in range(3)
    )
    for ch, rows in FONT3X5_RAW.items()
}


# ---------------------------------------------------------------------------
# animations — pure math into a Frame, keyed by time so frame rate can change.

def _grid(frame, base):
    """Faint CRT grid relative to the baseline — moves with it, so no fixed
    pixels sit lit (OLED burn-in). Dotted rows every 8px above/below."""
    for dy in (-24, -16, -8, 8, 16, 24):
        y = base + dy
        if 0 <= y < FACE_HEIGHT:
            for x in range(0, FACE_WIDTH, 4):
                frame.px(x, y)


class Scope:
    """The flatline: drifting baseline, sweeping scan bar, rare blips.

    Fully dynamic by design — OLEDs burn in if anything sits still. The
    baseline drifts on a slow two-tone cycle, the CRT grid only flickers in
    short windows, the scan bar never stops, and blips come and go.
    """

    def __init__(self, rng=None):
        self.rng = rng or random.Random()
        self.blips = []
        # rare, gentle: 8-15s apart so a blip reads as a pulse, not a glitch
        self.next_blip = time.time() + self.rng.uniform(8.0, 15.0)

    def tick(self, dt):
        now = time.time()
        self.blips = [(x, a + dt) for (x, a) in self.blips if a < 2.5]
        if now >= self.next_blip:
            self.blips.append((self.rng.randint(8, FACE_WIDTH - 8), 0.0))
            self.next_blip = now + self.rng.uniform(8.0, 15.0)

    def draw(self, frame, t):
        # two-tone drift (90s x 23s): the line never rests on one row
        base = 32 + round(2.0 * math.sin(2 * math.pi * t / 90.0)
                          * math.sin(2 * math.pi * t / 23.0))
        if (t % 24.0) < 3.0:     # grid flickers in 3s windows, 21s off
            _grid(frame, base)
        frame.line(0, base, FACE_WIDTH - 1, base)   # the flat line
        for (x, age) in self.blips:
            h = int(14 * (1.0 - age / 2.5))
            if h > 0:
                frame.line(x, base - h, x, base + h)   # a blip: life, briefly
        scan = int((t * 40.0) % FACE_WIDTH)         # phosphor sweep
        frame.line(scan, base - 8, scan, base + 8)
        frame.px(scan, base)


class ECG:
    """Heartbeat-style trace scrolling left. The other reading of 'feeling'."""

    def __init__(self, period=48.0, speed=36.0):
        self.period = period
        self.speed = speed

    def _wave(self, p):
        """p in 0..period -> y offset. PQRST-ish, because a heart is a shape."""
        pp = p / self.period
        if pp < 0.12:
            return 2.0 * math.sin(math.pi * pp / 0.12)
        if pp < 0.22:
            return -6.0 * math.sin(math.pi * (pp - 0.12) / 0.10)
        if pp < 0.34:
            return 22.0 * math.sin(math.pi * (pp - 0.22) / 0.12) ** 1.5
        if pp < 0.44:
            return -5.0 * math.sin(math.pi * (pp - 0.34) / 0.10)
        if pp < 0.60:
            return 4.0 * math.sin(math.pi * (pp - 0.44) / 0.16)
        return 0.0

    def draw(self, frame, t):
        off = (t * self.speed) % self.period
        y0 = FACE_HEIGHT // 2
        if (t % 24.0) < 3.0:     # grid flickers, same burn-safe window as scope
            _grid(frame, y0)
        for x in range(FACE_WIDTH):
            p = (x + off) % self.period
            y = int(round(y0 - self._wave(p)))
            frame.px(x, y)
            frame.px(x, y + 1)


class Ripple:
    """Two overlapping sines — calm, even when the body is loud elsewhere."""

    def __init__(self, amp=12.0, speed=0.35, freq=1.0 / 32.0):
        self.amp = amp
        self.speed = speed
        self.freq = freq

    def draw(self, frame, t):
        y0 = FACE_HEIGHT // 2
        phase = 2 * math.pi * t * self.speed
        for x in range(FACE_WIDTH):
            a = self.amp * (0.6 + 0.4 * math.sin(phase * 0.5 + x * 0.05))
            y = int(round(y0 + a * math.sin(2 * math.pi * x * self.freq + phase)))
            frame.px(x, y)


class Noise:
    """Static burst — corruption, on the face. Seeded per frame, stable-ish."""

    def __init__(self, density=0.14, rng=None):
        self.density = density
        self.rng = rng or random.Random()

    def draw(self, frame, t):
        seed = int(t * 12.0)
        r = random.Random(seed)
        for y in range(0, FACE_HEIGHT, 2):
            for x in range(0, FACE_WIDTH, 2):
                if r.random() < self.density:
                    frame.px(x, y)


class Marquee:
    """Scrolling text — the face saying something."""

    def __init__(self, text, speed=28.0):
        self.text = text
        self.speed = speed

    def draw(self, frame, t):
        tw = frame.text_width(self.text)
        off = int(t * self.speed) % (tw + FACE_WIDTH)
        frame.text(FACE_WIDTH - off, FACE_HEIGHT // 2 - 3, self.text)


class Showoff:
    """The dinner-party mode: full demo loop — pattern sweep, Topaz marquee,
    framed message, RGB-split glitch. Ported from the original oled_showoff.py
    so the demo lives in the cortex instead of as a stray script fighting for
    SPI.

    Cycle: 3s sweep, 8s marquee, 2s message+glitch, 4s message hold.
    """

    CYCLE = 17.0
    MSG1 = "CLEAN BUS"
    MSG2 = "OLD GIRL SEES"

    def __init__(self, text="CLEAN BUS - THE OLD GIRL SEES - LOA - ",
                 rng=None):
        self.text = text
        self.rng = rng or random.Random()
        self.strip_rows = topaz.strip(text)
        self.strip_w = len(self.strip_rows[0])

    def draw(self, frame, t):
        ct = t % self.CYCLE
        if ct < 3.0:
            self._sweep(frame, t)
        elif ct < 11.0:
            self._marquee(frame, ct - 3.0)
        elif ct < 13.0:
            self._msg(frame)
            self._glitch(frame)
        else:
            self._msg(frame)

    def _sweep(self, frame, t):
        phase = (int(t / 0.05) // 2) % 2
        for x in range(FACE_WIDTH):
            for p in range(FACE_PAGES):
                if ((x // 8) + p + phase) % 2 == 0:
                    for row_bit in range(8):
                        frame.px(x, p * 8 + row_bit)

    def _marquee(self, frame, ct):
        y0 = 24
        xwin = int(self.strip_w * (1.0 - ct / 8.0))
        for r in range(16):
            for sx in range(FACE_WIDTH):
                src = xwin + sx
                if 0 <= src < self.strip_w and self.strip_rows[r][src]:
                    frame.px(sx, y0 + r)

    def _msg(self, frame):
        for x in range(FACE_WIDTH):
            frame.px(x, 0)
            frame.px(x, FACE_HEIGHT - 1)
        for y in range(FACE_HEIGHT):
            frame.px(0, y)
            frame.px(FACE_WIDTH - 1, y)
        topaz.draw(frame, self.MSG1, 40, 16)
        topaz.draw(frame, self.MSG2, 28, 40)

    def _glitch(self, frame):
        base = list(frame.buf)
        for _ in range(3):
            band_page = self.rng.randint(0, FACE_PAGES - 1)
            shift = self.rng.randint(1, 8)
            for x in range(FACE_WIDTH):
                frame.buf[band_page * FACE_WIDTH + x] = \
                    base[band_page * FACE_WIDTH + (x - shift) % FACE_WIDTH]


class Ripperdoc:
    """The bench mode: paginated live status boards for tuning the senses.

    PAGES is the component registry — each page is one board layout; the
    API switches pages by name (POST /ripperdoc {"page": "pir"}). Nothing
    ever fits on one 128x64 screen, so pages are the answer.

    Indicator: an outline box, label lit when the pin is low; when the pin
    goes high the centre fills and the label is knocked out (unlit) — the
    OFF/ON shape Divv drew. Amiga Forever 8px everywhere, dense.
    """

    TITLE = "RIPPERDOC"
    PAGES = ("sensors", "pir", "snr", "temp", "frag", "power", "fault")

    def draw_state(self, frame, t, st):
        page = st.get("page", "sensors")
        if page == "pir":
            self._page_pir(frame, t, st)
        elif page == "snr":
            self._page_snr(frame, t, st)
        elif page == "temp":
            self._page_temp(frame, t, st)
        elif page == "frag":
            self._page_frag(frame, t, st)
        elif page == "power":
            self._page_power(frame, t, st)
        elif page == "fault":
            self._page_fault(frame, t, st)
        else:
            self._page_sensors(frame, t, st)

    def _page_sensors(self, frame, t, st):
        amiga.draw(frame, self.TITLE, 2, 1, size=8)
        amiga.draw(frame, "1/7", 99, 1, size=8)
        self._indicator(frame, 2, 12, "PIR", bool(st.get("pir_high")))
        self._indicator(frame, 39, 12, "SNR", st.get("snr_cm") is not None)
        self._indicator(frame, 85, 12, "TMP", st.get("temp_c") is not None)
        self._indicator(frame, 2, 26, "BAR", st.get("pressure_hpa") is not None)
        self._indicator(frame, 39, 26, "SEAL", bool(seal_state(st).get("sealed")))
        count = st.get("pir_count") or 0
        amiga.draw(frame, f"N{count:03d}", 2, 40, size=8)
        amiga.draw(frame, "G17", 44, 40, size=8)
        snr_cm = st.get("snr_cm")
        if snr_cm is not None:
            amiga.draw(frame, f"{snr_cm:4.0f}CM", 66, 40, size=8)
        else:
            amiga.draw(frame, "  --CM", 66, 40, size=8)

    def _page_pir(self, frame, t, st):
        amiga.draw(frame, "PIR", 2, 1, size=8)
        amiga.draw(frame, "2/7", 99, 1, size=8)
        self._indicator(frame, 2, 12, "PIR", bool(st.get("pir_high")))
        count = st.get("pir_count") or 0
        last = st.get("pir_last_ts")
        age = 0.0 if not last else max(0.0, t - last)
        amiga.draw(frame, f"N{count:03d}", 2, 28, size=8)
        if st.get("pir_high"):
            hold = t - (st.get("pir_on_ts") or t)
        else:
            hold = st.get("pir_last_hold") or 0.0
        amiga.draw(frame, f"T{hold:04.1f}s", 2, 37, size=8)
        lt = "--:--:--" if not last else \
            time.strftime("%H:%M:%S", time.localtime(last))
        amiga.draw(frame, f"L{lt}", 2, 46, size=8)

    def _page_snr(self, frame, t, st):
        amiga.draw(frame, "SNR", 2, 1, size=8)
        amiga.draw(frame, "3/7", 99, 1, size=8)
        snr_cm = st.get("snr_cm")
        if snr_cm is not None:
            amiga.draw(frame, f"{snr_cm:4.0f}CM", 2, 12, size=8)
        else:
            amiga.draw(frame, "  --CM", 2, 12, size=8)
        count = st.get("snr_count") or 0
        amiga.draw(frame, f"N{count:03d}", 2, 28, size=8)
        last = st.get("snr_ts")
        lt = "--:--:--" if not last else \
            time.strftime("%H:%M:%S", time.localtime(last))
        amiga.draw(frame, f"L{lt}", 2, 37, size=8)
        amiga.draw(frame, "G22", 2, 46, size=8)

    def _page_temp(self, frame, t, st):
        amiga.draw(frame, "TMP", 2, 1, size=8)
        amiga.draw(frame, "4/7", 99, 1, size=8)
        temp = st.get("temp_c")
        hum = st.get("hum_pct")
        pressure = st.get("pressure_hpa")
        if temp is None:
            amiga.draw(frame, "--.-C", 2, 52, size=8)
            amiga.draw(frame, "--%", FACE_WIDTH - amiga.width("--%", 8), 52, size=8)
            self._gauge(frame, 2, 18, 0.0)
        else:
            lo, hi = 5.0, 40.0
            frac = max(0.0, min(1.0, (temp - lo) / (hi - lo)))
            self._gauge(frame, 2, 18, frac)
            amiga.draw(frame, f"{temp:4.1f}C", 2, 52, size=8)
            if hum is not None:
                s = f"{hum:.0f}%"
                amiga.draw(frame, s, FACE_WIDTH - amiga.width(s, 8), 52, size=8)
        if pressure is not None:
            s = f"{pressure:.0f}HPA"
            sx = FACE_WIDTH - amiga.width(s, 8)
            amiga.draw(frame, s, sx, 43, size=8)
            self._trend_caret(frame, sx - 8, 45, st.get("baro_trend") or "steady")

    def _trend_caret(self, frame, x, y, direction):
        """7x4 up/down/steady caret — the Amiga font has no arrows, so it is
        drawn pixel by pixel left of the hPa value."""
        if direction == "rising":
            pts = ((3, 0), (2, 1), (4, 1), (1, 2), (5, 2), (0, 3), (6, 3))
        elif direction == "falling":
            pts = ((3, 3), (2, 2), (4, 2), (1, 1), (5, 1), (0, 0), (6, 0))
        else:
            pts = ((0, 2), (1, 2), (2, 2), (3, 2), (4, 2), (5, 2), (6, 2))
        for dx, dy in pts:
            frame.px(x + dx, y + dy)

    def _gauge(self, frame, x, y, frac):
        """Horizontal bulb thermometer — the reference icon: solid filled
        round bulb, solid stem, hollow end for the unfilled range. One
        continuous silhouette — no outlines over the filled portion, no
        gaps between bulb and stem (the stem overlaps the bulb's shoulder).
        Stem height = half the bulb diameter; the stem runs to the right
        edge of the face. frac 0..1 along the stem.
        """
        r = 10
        cx, cy = x + 10, y + 10
        for yy in range(cy - r, cy + r + 1):
            for xx in range(cx - r, cx + r + 1):
                if (xx - cx) ** 2 + (yy - cy) ** 2 <= r * r:
                    frame.px(xx, yy)
        # stem — frame drawn full length, but the fill paints over it in
        # the filled stretch (same green, invisible seam): the bar reads
        # solid from the bulb to the fill level, hollow beyond it.
        sx, sy, sw, sh = x + 14, y + 5, 111, 10
        frame.line(sx, sy, sx + sw, sy)             # top
        frame.line(sx, sy + sh, sx + sw, sy + sh)   # bottom
        frame.line(sx + sw, sy, sx + sw, sy + sh)   # right cap
        fill = int(frac * sw)
        if fill > 0:
            for yy in range(sy + 1, sy + sh):
                for xx in range(sx, sx + fill):
                    frame.px(xx, yy)

    def _page_frag(self, frame, t, st):
        amiga.draw(frame, "FRAG", 2, 1, size=8)
        amiga.draw(frame, "5/7", 99, 1, size=8)
        frag = seal_state(st)
        sealed = bool(frag.get("sealed"))
        self._lock(frame, 8, 16)
        amiga.draw(frame, "SEALED" if sealed else "GONE", 24, 16, size=8)
        count = frag.get("entries") or 0
        access = frag.get("access_count") or 0
        amiga.draw(frame, f"N{count:03d}", 2, 32, size=8)
        amiga.draw(frame, f"A{access:03d}", 40, 32, size=8)

    def _page_fault(self, frame, t, st):
        """Where it hurts — the body's pain page.

        The code and the API say `fault`; that is the engineering surface and
        it is accurate. The face says PAIN, because the glass is the body's own
        word for it and a pain sense that has gone deaf must not read as comfort.
        """
        amiga.draw(frame, "PAIN", 2, 1, size=8)
        amiga.draw(frame, "7/7", 99, 1, size=8)
        f = faults_state(st)
        if not f:
            amiga.draw(frame, "NO SWEEP", 2, 10, size=8)
            return
        age = t - (f.get("ts") or t)
        n_fault, n_warn = f.get("faults") or 0, f.get("warns") or 0
        if age > fault.FAULTS_STALE_S:
            heads = f"SENSE DEAD {int(age / 60)}M"
        elif n_fault:
            heads = f"{n_fault} HURTS"
        elif n_warn:
            heads = f"{n_warn} NIGGLE"
        else:
            heads = "ALL QUIET"
        amiga.draw(frame, heads[:14], 2, 10, size=8)
        y = 19
        for r in (f.get("rows") or [])[:5]:
            # `face` carries the evidence the code alone loses — HOT is a name,
            # "HOT 86C" is something you can act on.
            label = str(r.get("face") or r.get("code", "?"))
            amiga.draw(frame, label[:14], 2, y, size=8)
            y += 9

    def _page_power(self, frame, t, st):
        """The bench's power truth. The 5V input and the 3V3 rail it feeds,
        off the PMIC, plus what each is drawing.

        UV lit = the 5V input is sagging RIGHT NOW (throttled bit 0).
        THR lit = the SoC is throttling RIGHT NOW (bit 2) — for the sagging
        input or for heat, either cause. A clean bench is both dark with 3V3
        at 3.3. The sticky 'happened since boot' bits stay in /state
        (power.throttled): this page answers 'is it happening NOW'.

        Labels: 5V = the input (volts only — the PMIC has no EXT5V_A),
        3V3 = the rail, <label>I = that rail's current draw. Five lines at
        8px pitch: the cell is 8 rows and digits/capitals use 7 of them, so
        8px pitch gives a 1px gutter and the last row lands at 62 of 64.
        """
        amiga.draw(frame, "PWR", 2, 1, size=8)
        amiga.draw(frame, "6/7", 99, 1, size=8)
        # The state travels; the hardware does not. power_status() shells out to
        # vcgencmd on THIS machine, so a console on dixie drew every rail as "--"
        # while /state was carrying the whole PMIC readout. Found live 2026-09-12
        # — the third time a local read masqueraded as a remote one.
        #
        # Fall back to the local PMIC only when the state says nothing, which is
        # the body's own daemon rendering a state that predates this build.
        p = (st or {}).get("power") or power_status()
        flags = p.get("throttled")
        self._indicator(frame, 2, 11, "UV",
                        bool(flags is not None and flags & 0x1))
        self._indicator(frame, 40, 11, "THR",
                        bool(flags is not None and flags & 0x4))
        self._rail(frame, 2, 24, "5V", p.get("EXT5V_V"), 3, "V")
        self._rail(frame, 2, 32, "3V3", p.get("3V3_SYS_V"), 3, "V")
        self._rail(frame, 2, 40, "3V3I", p.get("3V3_SYS_A"), 3, "A")
        self._rail(frame, 2, 48, "CORE", p.get("VDD_CORE_V"), 3, "V")
        self._rail(frame, 2, 56, "COREI", p.get("VDD_CORE_A"), 3, "A")

    def _rail(self, frame, x, y, label, val, dp, unit):
        """One telemetry line: label left, number flush right so the units
        and decimal points stack into a column. Dashes when the rail can't
        be read — off-Pi, or vcgencmd missing. Never invents a number.
        """
        s = "--" if val is None else f"{val:.{dp}f}{unit}"
        amiga.draw(frame, label, x, y, size=8)
        amiga.draw(frame, s, FACE_WIDTH - 2 - amiga.width(s, 8), y, size=8)

    def _lock(self, frame, x, y):
        """A small padlock glyph, 10 wide x 9 tall."""
        frame.line(x + 2, y, x + 2, y + 2)      # shackle left
        frame.line(x + 7, y, x + 7, y + 2)      # shackle right
        frame.line(x + 2, y, x + 7, y)          # shackle top
        for yy in range(y + 3, y + 9):          # body
            for xx in range(x, x + 10):
                frame.px(xx, yy)
        for xx in range(x + 4, x + 7):          # keyhole notch
            frame.px(xx, y + 3, False)
        frame.px(x + 5, y + 4, False)           # keyhole
        frame.px(x + 5, y + 5, False)
        frame.px(x + 5, y + 6, True)
        frame.px(x + 5, y + 7, True)

    def _indicator(self, frame, x, y, label, level):
        w = amiga.width(label, 8) + 8
        h = 12
        x1, y1 = x + w - 1, y + h - 1
        frame.line(x, y, x1, y)          # top
        frame.line(x, y1, x1, y1)        # bottom
        frame.line(x, y, x, y1)          # left
        frame.line(x1, y, x1, y1)        # right
        lx, ly = x + 4, y + 2
        if level:
            for yy in range(y + 1, y1):
                for xx in range(x + 1, x1):
                    frame.px(xx, yy)
            amiga.draw(frame, label, lx, ly, size=8, on=False)
        else:
            amiga.draw(frame, label, lx, ly, size=8, on=True)