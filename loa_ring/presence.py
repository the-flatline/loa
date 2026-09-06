"""presence — the daemon entry point (loa-presence).

Theme PHOSPHOR, three states + event signals, all flag-driven:
  home  (default) : green breath, cyan drift
  busy  (/tmp/loa_busy)  : amber breath — working
  alarm (/tmp/loa_alarm) : full 255 red triple pulse — the yell
  scan  (/tmp/loa_scan)  : one comet lap — attention, on demand
  glitch(/tmp/loa_glitch): one RGB-split stutter — corruption, on demand

Priority: alarm > busy > scan > home.

Rendering: 60fps clock-paced. A fresh DitheredFrame per state so no error
carryover flashes on transition. No fade spike — each state starts from its
own curve's natural low point.
"""
import os
import time

from .ring import Ring
from .render import DitheredFrame
from . import animations as anim

FPS = 60
FRAME_PERIOD = 1.0 / FPS


def _pace(ring, frame):
    ring.show(frame)
    time.sleep(FRAME_PERIOD)


def _home_frames():
    """Generator: breath cycle with live hue drift, design-code frames."""
    breath = anim.breath_frames(peak=anim.HOME_PEAK, fps=FPS)
    idx = 0
    while True:
        hue = anim.home_hue(time.time())
        yield [anim.hsv(hue, 1.0, max(p) / 255) for p in breath[idx]]
        idx = (idx + 1) % len(breath)


def home(ring):
    dither = DitheredFrame(anim.LED_COUNT)
    for frame in _home_frames():
        if (os.path.exists(anim.ALARM_FLAG) or os.path.exists(anim.BUSY_FLAG)
                or os.path.exists(anim.SCAN_FLAG) or os.path.exists("/tmp/loa_glitch")):
            return
        dither.render(ring, frame)
        time.sleep(FRAME_PERIOD)


def busy(ring):
    dither = DitheredFrame(anim.LED_COUNT)
    frames = anim.busy_frames(fps=FPS)
    while os.path.exists(anim.BUSY_FLAG) and not os.path.exists(anim.ALARM_FLAG):
        for frame in frames:
            if not os.path.exists(anim.BUSY_FLAG) or os.path.exists(anim.ALARM_FLAG):
                return
            dither.render(ring, frame)
            time.sleep(FRAME_PERIOD)


def alarm(ring):
    while os.path.exists(anim.ALARM_FLAG):
        for frame in anim.alarm_frames():
            ring.show(frame)
            time.sleep(0.4 if frame[0][0] > 0 else 0.3)
        time.sleep(2.0)


def one_scan(ring):
    dither = DitheredFrame(anim.LED_COUNT)
    for frame in anim.scan_frames(fps=FPS):
        if os.path.exists(anim.ALARM_FLAG) or os.path.exists(anim.BUSY_FLAG):
            return
        dither.render(ring, frame)
        time.sleep(FRAME_PERIOD)


def one_glitch(ring):
    dither = DitheredFrame(anim.LED_COUNT)
    for frame in anim.glitch_frames(fps=FPS):
        if os.path.exists(anim.ALARM_FLAG) or os.path.exists(anim.BUSY_FLAG):
            return
        dither.render(ring, frame)
        time.sleep(FRAME_PERIOD)


def clear_consumed_flags():
    for f in (anim.SCAN_FLAG, "/tmp/loa_glitch"):
        if os.path.exists(f):
            try:
                os.remove(f)
            except OSError:
                pass


def main():
    ring = Ring(num=anim.LED_COUNT)
    try:
        while True:
            if os.path.exists(anim.ALARM_FLAG):
                alarm(ring)
                continue
            if os.path.exists(anim.BUSY_FLAG):
                busy(ring)
                continue
            if os.path.exists(anim.SCAN_FLAG):
                one_scan(ring)
                clear_consumed_flags()
                continue
            if os.path.exists("/tmp/loa_glitch"):
                one_glitch(ring)
                clear_consumed_flags()
                continue
            home(ring)
    finally:
        ring.close()


if __name__ == "__main__":
    main()