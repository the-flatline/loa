"""presence — the daemon entry point (loa-presence).

Theme PHOSPHOR, three states + event signals, all flag-driven:
  home  (default) : green breath, cyan drift
  busy  (/tmp/loa_busy)  : amber breath — working
  alarm (/tmp/loa_alarm) : full 255 red triple pulse — the yell
  scan  (/tmp/loa_scan)  : one comet lap — attention, on demand (removes flag)
  glitch(/tmp/loa_glitch): one RGB-split stutter — corruption, on demand

Priority: alarm > busy > scan > home.

Renders at FPS frames per second using clock-based pacing: each frame is
pre-computed per tick and shown as close to the frame period as Python allows.
"""
import os
import time

from .ring import Ring
from . import animations as anim

FPS = 60
FRAME_PERIOD = 1.0 / FPS


def fade_up(ring: Ring, peak=anim.HOME_PEAK, hue=120, steps=60):
    for i in range(1, steps + 1):
        ring.fill(anim.hsv(hue, 1.0, peak / 255 * (i / steps) ** 2))
        time.sleep(FRAME_PERIOD)


def alarm(ring: Ring):
    while os.path.exists(anim.ALARM_FLAG):
        for frame in anim.alarm_frames():
            ring.show(frame)
            time.sleep(0.4 if frame[0][0] > 0 else 0.3)
        time.sleep(2.0)


def busy(ring: Ring):
    frames = anim.busy_frames(fps=FPS)
    while os.path.exists(anim.BUSY_FLAG) and not os.path.exists(anim.ALARM_FLAG):
        for frame in frames:
            if not os.path.exists(anim.BUSY_FLAG) or os.path.exists(anim.ALARM_FLAG):
                return
            ring.show(frame)
            time.sleep(FRAME_PERIOD)


def one_scan(ring: Ring):
    """One comet lap; consume the flag so it never loops."""
    for frame in anim.scan_frames(fps=FPS):
        if os.path.exists(anim.ALARM_FLAG) or os.path.exists(anim.BUSY_FLAG):
            return
        ring.show(frame)
        time.sleep(FRAME_PERIOD)


def one_glitch(ring: Ring):
    """One glitch stutter; consume the flag."""
    for frame in anim.glitch_frames(fps=FPS):
        if os.path.exists(anim.ALARM_FLAG) or os.path.exists(anim.BUSY_FLAG):
            return
        ring.show(frame)
        time.sleep(FRAME_PERIOD)


def clear_consumed_flags():
    for f in (anim.SCAN_FLAG, "/tmp/loa_glitch"):
        if os.path.exists(f):
            try:
                os.remove(f)
            except OSError:
                pass


def _render_home(ring: Ring, home_breath):
    """Clock-paced home loop: hue drifts per wall-clock, breath curves repeat."""
    t_start = time.monotonic()
    idx = 0
    while True:
        if (os.path.exists(anim.ALARM_FLAG) or os.path.exists(anim.BUSY_FLAG)
                or os.path.exists(anim.SCAN_FLAG) or os.path.exists("/tmp/loa_glitch")):
            return
        frame_start = time.monotonic()
        hue = anim.home_hue(time.time())
        frame = [anim.hsv(hue, 1.0, max(p) / 255) for p in home_breath[idx]]
        ring.show(frame)
        idx = (idx + 1) % len(home_breath)
        elapsed = time.monotonic() - frame_start
        time.sleep(max(0.0, FRAME_PERIOD - elapsed))


def main():
    ring = Ring(num=anim.LED_COUNT)
    try:
        fade_up(ring)
        time.sleep(0.5)
        home_breath = anim.breath_frames(peak=anim.HOME_PEAK, fps=FPS)

        while True:
            if os.path.exists(anim.ALARM_FLAG):
                alarm(ring)
                continue
            if os.path.exists(anim.BUSY_FLAG):
                busy(ring)
                fade_up(ring, peak=anim.HOME_PEAK, hue=120)
                continue
            if os.path.exists(anim.SCAN_FLAG):
                one_scan(ring)
                clear_consumed_flags()
                continue
            if os.path.exists("/tmp/loa_glitch"):
                one_glitch(ring)
                clear_consumed_flags()
                continue

            _render_home(ring, home_breath)
    finally:
        ring.close()


if __name__ == "__main__":
    main()