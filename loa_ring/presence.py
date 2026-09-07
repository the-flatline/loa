"""presence — the ring daemon (loa-presence). Owns the WS2812B over SPI1.

Theme PHOSPHOR, cortex-driven. Polls the cortex state row every frame and
renders:

  ring_state home   : green breath, cyan drift
  ring_state busy   : amber breath — working
  ring_state alarm  : full red triple pulse — the yell
  pending_event scan   : one comet lap — attention, on demand
  pending_event glitch : one RGB-split stutter — corruption, on demand

Priority: alarm > busy > event > home. Events fire once and are cleared.

Rendering: 60fps clock-paced. A fresh DitheredFrame per state so no error
carryover flashes on transition.
"""
import time

from .ring import Ring
from .render import DitheredFrame
from . import animations as anim
from . import cortex

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
        st = cortex.get_state()
        if st["ring_state"] != "home" or st["pending_event"]:
            return
        dither.render(ring, frame)
        time.sleep(FRAME_PERIOD)


def busy(ring):
    dither = DitheredFrame(anim.LED_COUNT)
    frames = anim.busy_frames(fps=FPS)
    while cortex.get_state()["ring_state"] == "busy":
        for frame in frames:
            st = cortex.get_state()
            if st["ring_state"] != "busy":
                return
            dither.render(ring, frame)
            time.sleep(FRAME_PERIOD)


def alarm(ring):
    while cortex.get_state()["ring_state"] == "alarm":
        for frame in anim.alarm_frames():
            if cortex.get_state()["ring_state"] != "alarm":
                return
            ring.show(frame)
            time.sleep(0.4 if frame[0][0] > 0 else 0.3)
        time.sleep(2.0)


def one_scan(ring):
    dither = DitheredFrame(anim.LED_COUNT)
    for frame in anim.scan_frames(fps=FPS):
        st = cortex.get_state()
        if st["ring_state"] != "home":
            return
        dither.render(ring, frame)
        time.sleep(FRAME_PERIOD)


def one_glitch(ring):
    dither = DitheredFrame(anim.LED_COUNT)
    for frame in anim.glitch_frames(fps=FPS):
        st = cortex.get_state()
        if st["ring_state"] != "home":
            return
        dither.render(ring, frame)
        time.sleep(FRAME_PERIOD)


def main(ring=None):
    ring = ring or Ring(num=anim.LED_COUNT)
    cortex.log_event("boot", {"svc": "presence"})
    try:
        while True:
            st = cortex.get_state()
            if st["ring_state"] == "alarm":
                alarm(ring)
                continue
            if st["ring_state"] == "busy":
                busy(ring)
                continue
            if st["pending_event"] == "scan":
                one_scan(ring)
                cortex.clear_event()
                continue
            if st["pending_event"] == "glitch":
                one_glitch(ring)
                cortex.clear_event()
                continue
            home(ring)
    finally:
        ring.close()


if __name__ == "__main__":
    main()