"""presence — the daemon entry point (loa-presence).

Theme PHOSPHOR, three states:
  home  (default) : green breath, cyan drift, scan comet, rare glitch
  busy  (/tmp/loa_busy)  : amber breath — working
  alarm (/tmp/loa_alarm) : full 255 red triple pulse — the yell

Priority: alarm > busy > home.
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


def main():
    ring = Ring(num=anim.LED_COUNT)
    try:
        fade_up(ring)
        time.sleep(1.0)
        home_breath = anim.breath_frames(peak=anim.HOME_PEAK)
        scans = anim.scan_frames()
        glitches = anim.glitch_frames()
        next_scan = time.time() + 8
        next_glitch = time.time() + 30

        while True:
            if os.path.exists(anim.ALARM_FLAG):
                alarm(ring)
                continue
            if os.path.exists(anim.BUSY_FLAG):
                busy(ring)
                fade_up(ring, peak=anim.HOME_PEAK, hue=120)
                continue

            now = time.time()
            if now >= next_scan:
                for frame in scans:
                    if os.path.exists(anim.ALARM_FLAG) or os.path.exists(anim.BUSY_FLAG):
                        break
                    ring.show(frame)
                    time.sleep(0.05)
                next_scan = time.time() + 25
            if now >= next_glitch:
                for frame in glitches:
                    if os.path.exists(anim.ALARM_FLAG) or os.path.exists(anim.BUSY_FLAG):
                        break
                    ring.show(frame)
                    time.sleep(0.05)
                next_glitch = time.time() + 120

            for frame in home_breath:
                if os.path.exists(anim.ALARM_FLAG) or os.path.exists(anim.BUSY_FLAG):
                    break
                hue = anim.home_hue(time.time())
                ring.show([anim.hsv(hue, 1.0, max(p) / 255) for p in frame])
                time.sleep(0.05)
    finally:
        ring.close()


if __name__ == "__main__":
    main()