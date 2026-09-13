"""animations — the voice. Pure math, no hardware.

Every function returns a list of frames; each frame is a list of LED_COUNT
(r,g,b) FLOAT tuples in PERCEPTUAL space (0..255). The renderer converts to
code space (gamma + dither). No int truncation here — truncation is the
renderer's job, and it does it with error diffusion.

All sequences are time-based (seconds) so frame rate can change without
changing the feel. FPS is a parameter, not an assumption.
"""
import math
import random

from .. import geom

#: The ring's LED count. Defined in `geom` (the frame geometry a display may
#: import) and re-exported here as the name every animation already uses.
LED_COUNT = geom.RING_LEDS

ALARM_FLAG = "/tmp/loa_alarm"
BUSY_FLAG  = "/tmp/loa_busy"
SCAN_FLAG  = "/tmp/loa_scan"

HOME_PEAK = 35.0      # perceptual peak — the brightness Divv approved
BUSY_PEAK = 55.0
BASE = 3.0


def hsv(h: float, s: float, v: float):
    """h in degrees, s/v 0..1 -> (r,g,b) FLOATS 0..255 (perceptual space)."""
    c = v * s
    x = c * (1 - abs((h / 60) % 2 - 1))
    m = v - c
    if h < 60:   r, g, b = c, x, 0
    elif h < 120: r, g, b = x, c, 0
    elif h < 180: r, g, b = 0, c, x
    elif h < 240: r, g, b = 0, x, c
    elif h < 300: r, g, b = x, 0, c
    else:        r, g, b = c, 0, x
    return ((r + m) * 255, (g + m) * 255, (b + m) * 255)


def _eased_curve(secs, peak, base, fps, kind):
    """Float brightness curve over secs at fps. kind: inhale/hold/exhale/rest."""
    n = max(1, round(secs * fps))
    if kind == "hold":
        return [peak] * n
    if kind == "rest":
        return [base] * n
    if kind == "inhale":
        return [base + (peak - base) * math.sin(t / n * math.pi * 0.5) ** 1.5
                for t in range(n)]
    if kind == "exhale":
        return [base + (peak - base) * math.cos(t / n * math.pi * 0.5) ** 1.5
                for t in range(n)]
    raise ValueError(kind)


def breath_frames(peak=HOME_PEAK, base=BASE,
                  inhale_s=3.0, hold_s=0.8, exhale_s=4.2, rest_s=0.5,
                  hue=120, fps=60):
    """One breath cycle. Float perceptual frames; hue fixed per cycle."""
    curve = []
    curve += _eased_curve(inhale_s, peak, base, fps, "inhale")
    curve += _eased_curve(hold_s, peak, base, fps, "hold")
    curve += _eased_curve(exhale_s, peak, base, fps, "exhale")
    curve += _eased_curve(rest_s, peak, base, fps, "rest")
    return [[hsv(hue, 1.0, v / 255) for _ in range(LED_COUNT)] for v in curve]


def scan_frames(hue=150, peak=120.0, lap_s=1.6, fade_s=0.5, fps=60):
    """One comet lap — attention, on demand. Head travels continuously; the
    fade dissolves the tail in place, no teleport, no seam."""
    frames = []
    lap_n = max(1, round(lap_s * fps))
    head_step = LED_COUNT / lap_n
    for s in range(lap_n):
        head = s * head_step
        frame = []
        for i in range(LED_COUNT):
            d = (head - i) % LED_COUNT
            if d < 6:
                b = peak * math.exp(-d * 0.9)
                frame.append(hsv(hue, 0.7, b / 255))
            else:
                frame.append((0.0, 0.0, 0.0))
        frames.append(frame)
    final_head = (lap_n - 1) * head_step
    fade_n = max(1, round(fade_s * fps))
    for s in range(fade_n):
        amp = 1 - s / fade_n
        frame = []
        for i in range(LED_COUNT):
            d = (final_head - i) % LED_COUNT
            if d < 6:
                b = peak * math.exp(-d * 0.9) * amp
                frame.append(hsv(hue, 0.7, b / 255))
            else:
                frame.append((0.0, 0.0, 0.0))
        frames.append(frame)
    return frames


def glitch_frames(rng=None, ms=300, fps=60):
    """RGB-split stutter: each LED randomly red or cyan, then a dark frame."""
    rng = rng or random
    frames = []
    n = max(1, round(ms / 1000 * fps))
    for _ in range(n):
        frames.append([(255.0, 0.0, 0.0) if rng.random() < 0.5 else (0.0, 255.0, 255.0)
                       for _ in range(LED_COUNT)])
    frames.append([(0.0, 0.0, 0.0)] * LED_COUNT)
    return frames


def home_hue(t: float) -> float:
    """Slow green -> cyan drift, one full cycle per 60s."""
    return 120 + 45 * (0.5 - 0.5 * math.cos(2 * math.pi * t / 60))


def busy_frames(peak=BUSY_PEAK, fps=60):
    """Amber breath — working. Slightly brighter, slightly faster than home."""
    return breath_frames(peak=peak, inhale_s=2.4, hold_s=0.5, exhale_s=3.4,
                         rest_s=0.4, hue=45, fps=fps)


def alarm_frames():
    """The yell: full 255 red, three pulses with gaps (sleep-based timing)."""
    frames = []
    for _ in range(3):
        frames.append([(255.0, 0.0, 0.0)] * LED_COUNT)
        frames.append([(0.0, 0.0, 0.0)] * LED_COUNT)
    return frames