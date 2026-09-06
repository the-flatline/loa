"""animations — the voice. Pure math, no hardware.

Every function returns a list of frames; each frame is a list of LED_COUNT
(r,g,b) tuples. Render them with Ring.show(), print them with loa_sim, or
test them anywhere.
"""
import math
import random

LED_COUNT = 24

ALARM_FLAG = "/tmp/loa_alarm"
BUSY_FLAG  = "/tmp/loa_busy"
SCAN_FLAG  = "/tmp/loa_scan"

HOME_PEAK = 35
BUSY_PEAK = 55
BASE = 3


def hsv(h: float, s: float, v: float):
    """h in degrees, s/v 0..1 -> (r,g,b) 0..255"""
    c = v * s
    x = c * (1 - abs((h / 60) % 2 - 1))
    m = v - c
    if h < 60:   r, g, b = c, x, 0
    elif h < 120: r, g, b = x, c, 0
    elif h < 180: r, g, b = 0, c, x
    elif h < 240: r, g, b = 0, x, c
    elif h < 300: r, g, b = x, 0, c
    else:        r, g, b = c, 0, x
    return (int((r + m) * 255), int((g + m) * 255), int((b + m) * 255))


def breath_frames(peak=HOME_PEAK, base=BASE,
                  inhale=60, hold=16, exhale=84, rest=10, hue=120):
    """One breath cycle. Inhale (eased), hold, exhale (eased), rest."""
    curve = []
    for i in range(inhale):
        t = i / inhale
        curve.append(base + (peak - base) * math.sin(t * math.pi * 0.5) ** 1.5)
    curve += [peak] * hold
    for i in range(exhale):
        t = i / exhale
        curve.append(base + (peak - base) * math.cos(t * math.pi * 0.5) ** 1.5)
    curve += [base] * rest
    return [[hsv(hue, 1.0, v / 255) for _ in range(LED_COUNT)] for v in curve]


def scan_frames(hue=150, peak=120, lap_steps=47, fade_steps=10):
    """One comet lap — an ATTENTION signal, fired on demand, not random.
    The head travels continuously around the ring (it's a circle, no seam,
    no teleport). Tail is an exponential falloff BEHIND the head. The lap
    ends with the head at a clean LED position; the fade dissolves the tail
    in place with zero extra motion — the comet stops where it stops and
    fades there."""
    frames = []
    head_step = 24 / 48                 # 0.5 LED per frame
    for s in range(lap_steps):
        head = s * head_step            # 0 -> 23.0
        frame = []
        for i in range(LED_COUNT):
            d = (head - i) % LED_COUNT  # distance BEHIND head, circular
            if d < 6:
                b = peak * math.exp(-d * 0.9)
                frame.append(hsv(hue, 0.7, b / 255))
            else:
                frame.append((0, 0, 0))
        frames.append(frame)
    final_head = (lap_steps - 1) * head_step   # where the head actually stopped
    for s in range(fade_steps):
        amp = 1 - s / fade_steps
        frame = []
        for i in range(LED_COUNT):
            d = (final_head - i) % LED_COUNT
            if d < 6:
                b = peak * math.exp(-d * 0.9) * amp
                frame.append(hsv(hue, 0.7, b / 255))
            else:
                frame.append((0, 0, 0))
        frames.append(frame)
    return frames


def glitch_frames(rng=None, frames_n=6):
    """RGB-split stutter: each LED randomly red or cyan, then a dark frame."""
    rng = rng or random
    frames = []
    for _ in range(frames_n):
        frames.append([(255, 0, 0) if rng.random() < 0.5 else (0, 255, 255)
                       for _ in range(LED_COUNT)])
    frames.append([(0, 0, 0)] * LED_COUNT)
    return frames


def home_hue(t: float) -> float:
    """Slow green -> cyan drift, one full cycle per 60s."""
    return 120 + 45 * (0.5 - 0.5 * math.cos(2 * math.pi * t / 60))


def busy_frames():
    """Amber breath — working. Slightly brighter, slightly faster than home."""
    return breath_frames(peak=BUSY_PEAK, inhale=48, hold=10, exhale=68, rest=8,
                         hue=45)


def alarm_frames():
    """The yell: full 255 red, three pulses with gaps."""
    frames = []
    for _ in range(3):
        frames.append([(255, 0, 0)] * LED_COUNT)
        frames.append([(0, 0, 0)] * LED_COUNT)
    return frames