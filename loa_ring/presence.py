"""presence — the daemon entry point (loa-presence).

Theme PHOSPHOR, three states + event signals, all flag-driven:
  home  (default) : green breath, cyan drift
  busy  (/tmp/loa_busy)  : amber breath — working
  alarm (/tmp/loa_alarm) : full 255 red triple pulse — the yell
  scan  (/tmp/loa_scan)  : one comet lap — attention, on demand (removes flag)
  glitch(/tmp/loa_glitch): one RGB-split stutter — corruption, on demand

Priority: alarm > busy > scan > home.
"""
import os
import time

from .ring import Ring
from . import animations as anim


def fade_up(ring: Ring, peak=anim.HOME_PEAK, hue=120, steps=40):
    for i in range(1, steps + 1):
        ring.fill(anim.hsv(hue, 1.0, peak / 255 * (i / steps) ** 2))
        time.sleep(0.05)


def alarm(ring: Ring):
    while os.path.exists(anim.ALARM_FLAG):
        for frame in anim.alarm_frames():
            ring.show(frame)
            time.sleep(0.4 if frame[0][0] > 0 else 0.3)
        time.sleep(2.0)


def busy(ring: Ring):
    frames = anim.busy_frames()
    while os.path.exists(anim.BUSY_FLAG) and not os.path.exists(anim.ALARM_FLAG):
        for frame in frames:
            if not os.path.exists(anim.BUSY_FLAG) or os.path.exists(anim.ALARM_FLAG):
                return
            ring.show(frame)
            time.sleep(0.05)


def one_scan(ring: Ring):
    """One comet lap; consume the flag so it never loops."""
    for frame in anim.scan_frames():
        if os.path.exists(anim.ALARM_FLAG) or os.path.exists(anim.BUSY_FLAG):
            return
        ring.show(frame)
        time.sleep(0.05)


def one_glitch(ring: Ring):
    """One glitch stutter; consume the flag."""
    for frame in anim.glitch_frames():
        if os.path.exists(anim.ALARM_FLAG) or os.path.exists(anim.BUSY_FLAG):
            return
        ring.show(frame)
        time.sleep(0.05)


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
        fade_up(ring)
        time.sleep(1.0)
        home_breath = anim.breath_frames(peak=anim.HOME_PEAK)

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

            hue = anim.home_hue(time.time())
            for frame in home_breath:
                if (os.path.exists(anim.ALARM_FLAG) or os.path.exists(anim.BUSY_FLAG)
                        or os.path.exists(anim.SCAN_FLAG) or os.path.exists("/tmp/loa_glitch")):
                    break
                ring.show([anim.hsv(hue, 1.0, max(p) / 255) for p in frame])
                time.sleep(0.05)
    finally:
        ring.close()


if __name__ == "__main__":
    main()