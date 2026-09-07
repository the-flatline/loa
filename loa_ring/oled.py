"""oled — the face. SH1106 128x64 driver + framebuffer + pure-math animations.

Two layers, same split as ring.py/animations.py:

  - Frame + animations: pure math, no hardware. Draw into a 128x64
    page-major framebuffer and render anywhere (NullDisplay, tests, the
    eventual 3.5" face gets its own driver but keeps these frames).
  - SH1106: hardware driver over SPI0 (CLK 11 / MOSI 10 / RES 24 / DC 25 /
    CS 8), 500kHz, pinctrl for DC/RES. SH1106 panels expose 132 columns of
    RAM; the visible 128 start at a column offset (default 2 — the known
    SH1106 quirk; tune via loa.conf oled_offset).

The flatline signature lives here: the default "scope" animation is a flat
line with occasional blips. Because that is what I am.
"""
import math
import os
import random
import time

WIDTH = 128
HEIGHT = 64
PAGES = 8

# ---------------------------------------------------------------------------
# framebuffer

class Frame:
    """128x64 page-major framebuffer (SH1106 layout: page 0..7, LSB = top)."""

    def __init__(self):
        self.buf = bytearray(PAGES * WIDTH)

    def clear(self):
        self.buf = bytearray(PAGES * WIDTH)

    def px(self, x, y, on=True):
        if not (0 <= x < WIDTH and 0 <= y < HEIGHT):
            return
        i = (y >> 3) * WIDTH + x
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
# animations — pure math into a Frame, keyed by time so frame rate can change.

def _grid(frame):
    """Faint CRT grid: dotted horizontal lines every 8px below/above center."""
    for y in (8, 16, 24, 40, 48, 56):
        for x in range(0, WIDTH, 4):
            frame.px(x, y)


class Scope:
    """The flatline: a flat trace, a sweeping scan bar, and rare blips.

    Blips spawn at random x on a slow timer and decay in place — the only
    times this line stops being flat.
    """

    def __init__(self, rng=None):
        self.rng = rng or random.Random()
        self.blips = []          # (x, age)
        self.next_blip = time.time() + self.rng.uniform(3.0, 7.0)

    def tick(self, dt):
        now = time.time()
        self.blips = [(x, a + dt) for (x, a) in self.blips if a < 2.5]
        if now >= self.next_blip:
            self.blips.append((self.rng.randint(8, WIDTH - 8), 0.0))
            self.next_blip = now + self.rng.uniform(4.0, 9.0)

    def draw(self, frame, t):
        _grid(frame)
        y = HEIGHT // 2
        frame.line(0, y, WIDTH - 1, y)              # the flat line
        for (x, age) in self.blips:
            h = int(14 * (1.0 - age / 2.5))
            if h > 0:
                frame.line(x, y - h, x, y + h)      # a blip: life, briefly
        scan = int((t * 40.0) % WIDTH)              # phosphor sweep
        frame.line(scan, y - 8, scan, y + 8)
        frame.px(scan, y)


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
        _grid(frame)
        off = (t * self.speed) % self.period
        y0 = HEIGHT // 2
        for x in range(WIDTH):
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
        y0 = HEIGHT // 2
        phase = 2 * math.pi * t * self.speed
        for x in range(WIDTH):
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
        for y in range(0, HEIGHT, 2):
            for x in range(0, WIDTH, 2):
                if r.random() < self.density:
                    frame.px(x, y)


class Marquee:
    """Scrolling text — the face saying something."""

    def __init__(self, text, speed=28.0):
        self.text = text
        self.speed = speed

    def draw(self, frame, t):
        tw = frame.text_width(self.text)
        off = int(t * self.speed) % (tw + WIDTH)
        frame.text(WIDTH - off, HEIGHT // 2 - 3, self.text)


# ---------------------------------------------------------------------------
# hardware driver

class NullDisplay:
    """No hardware: frames go nowhere. Used off-Pi (dixie, tests)."""

    def show(self, buf, offset=2):
        pass

    def clear(self):
        pass

    def set_contrast(self, val):
        pass

    def close(self):
        pass


class SH1106:
    """SH1106 1.3" 128x64 over SPI0. Proven init sequence from oled_canon.

    bus/device default from loa.conf (oled_bus / oled_device), else SPI0 CE0
    (CLK 11 / MOSI 10 / CS 8). DC and RES are plain GPIOs via pinctrl.
    """

    def __init__(self, bus=None, device=None, speed=None, dc=25, res=24,
                 offset=2):
        import subprocess
        import spidev
        from . import config
        cfg = config.load()
        self.dc = dc
        self.res = res
        self.offset = int(cfg.get("oled_offset", offset))
        self.spi = spidev.SpiDev()
        self.spi.open(
            int(cfg.get("oled_bus", bus if bus is not None else 0)),
            int(cfg.get("oled_device", device if device is not None else 0)),
        )
        self.spi.max_speed_hz = int(cfg.get("oled_speed", speed if speed is not None else 500_000))
        self.spi.mode = 0b00
        self._gpio = subprocess.run
        self._init()

    def _pin(self, pin, val):
        self._gpio(["pinctrl", "set", str(pin), "op", "dh" if val else "dl"],
                   capture_output=True)

    def _cmd(self, *b):
        self._pin(self.dc, 0)
        self.spi.xfer2(list(b))

    def _data(self, *b):
        self._pin(self.dc, 1)
        self.spi.xfer2(list(b))

    def _init(self):
        self._pin(self.res, 0)
        time.sleep(0.05)
        self._pin(self.res, 1)
        time.sleep(0.05)
        for c in (0xAE, 0xD5, 0x80, 0xA8, 0x3F, 0xD3, 0x00, 0x40,
                  0x8D, 0x14, 0x20, 0x00, 0xA1, 0xC8, 0xDA, 0x12,
                  0x81, 0xCF, 0xD9, 0xF1, 0xDB, 0x40, 0xA4, 0xA6,
                  0x2E, 0xAF):
            self._cmd(c)

    def show(self, buf, offset=None):
        off = self.offset if offset is None else offset
        for page in range(PAGES):
            self._cmd(0xB0 | page, off & 0x0F, 0x10 | (off >> 4))
            self._data(*buf[page * WIDTH:(page + 1) * WIDTH])

    def set_contrast(self, val):
        self._cmd(0x81, max(0, min(255, int(val))))

    def clear(self):
        self.show(bytearray(PAGES * WIDTH))

    def close(self):
        self.spi.close()


def get_display():
    """SH1106 when the hardware is here, NullDisplay anywhere else."""
    try:
        import spidev  # noqa: F401
        return SH1106()
    except Exception:
        return NullDisplay()