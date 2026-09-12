"""presence — the ring daemon (loa-presence). Owns the WS2812B over SPI1.

Theme PHOSPHOR, cortex-driven. Polls the cortex state row every frame and
renders:

  ring_state home   : green breath, cyan drift
  ring_state busy   : amber breath — working
  ring_state alarm  : full red triple pulse — the yell
  pending_event scan   : one comet lap — attention, on demand
  pending_event glitch : one RGB-split stutter — corruption, on demand

Priority: alarm > hurts > mute > busy > event > home. Events fire once and are
cleared.

The ring is the body's INVOLUNTARY tell — the ears and the tail. It carries
how I am without words, and it keeps working when the face cannot: a dead
face is a black rectangle and that black rectangle is the signal, so the ring
has to be the one that says so.

Condition (from the fault sweep) outranks the cosmetic moods. Being busy is
decoration; hurting is information. Only an explicit alarm outranks it.

  well   : the green breath, unchanged
  niggle : one amber tick folded into the breath, rare enough to mean something
  hurts  : the breath STOPS and it holds a deep red pulse
  mute   : the sweep itself is dead — dark, one dim blink every 10s, alive but
           unable to speak. Silence must never look like calm.

Rendering: 60fps clock-paced. A fresh DitheredFrame per state so no error
carryover flashes on transition.
"""
import time

from .ws2812 import Ring
from .render import DitheredFrame
from . import animations as anim
from . import cortex
from . import fault

FPS = 60
FRAME_PERIOD = 1.0 / FPS
NIGGLE_PERIOD_S = 20.0

_COND: dict = {"ts": 0.0, "val": "well"}


def _condition(ttl=1.0) -> str:
    """The body's own report, polled at most once a second — this is called
    every frame. A broken sweep must read as mute, never as well."""
    now = time.time()
    if now - _COND["ts"] < ttl:
        return _COND["val"]
    try:
        val = fault.condition()
    except Exception:                                       # noqa: BLE001
        val = "mute"
    _COND["ts"], _COND["val"] = now, val
    return val


def _solid(h: float, s: float, v: float):
    """One colour across the whole ring."""
    return [anim.hsv(h, s, v)] * anim.LED_COUNT


def hurting(ring):
    """HURTS: the breath stops and it holds a deep red pulse.

    Deliberately NOT the alarm triple-flash — alarm says 'look at me NOW',
    this says 'something is wrong inside me'. The tell is the stopped rhythm:
    a body in pain doesn't keep breathing evenly.
    """
    while _condition() == "hurts":
        for v in (0.85, 0.85, 0.85, 0.18, 0.18, 0.18, 0.18):
            if _condition() != "hurts":
                return
            ring.show(_solid(0.0, 1.0, v))
            time.sleep(0.28)


def muted(ring):
    """MUTE: the sweep itself is dead — dark, one dim blink every 10s.

    Alive but can't speak. The blink is the whole point: without it, a deaf
    body and an unplugged cable look identical.
    """
    while _condition() == "mute":
        ring.show(_solid(0.0, 0.0, 0.0))
        time.sleep(9.6)
        if _condition() != "mute":
            return
        ring.show(_solid(0.0, 0.0, 0.06))
        time.sleep(0.4)


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
    next_tick = time.time() + NIGGLE_PERIOD_S
    for frame in _home_frames():
        st = cortex.get_state()
        if st["ring_state"] != "home" or st["pending_event"]:
            return
        # a niggle rides the breath rather than replacing it — one amber tick,
        # rare enough to be information instead of noise
        if _condition() == "niggle" and time.time() >= next_tick:
            next_tick = time.time() + NIGGLE_PERIOD_S
            for v in (0.55, 0.10):
                if _condition() != "niggle":
                    break
                dither.render(ring, _solid(38.0, 1.0, v))
                time.sleep(0.18)
            continue
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
    # ripperdoc bench mode: snappier lap so the reaction reads instantly
    st = cortex.get_state()
    lap, fade = (0.9, 0.3) if st.get("ripperdoc") else (1.6, 0.5)
    for frame in anim.scan_frames(lap_s=lap, fade_s=fade, fps=FPS):
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
            cond = _condition()
            if st["ring_state"] == "alarm":
                alarm(ring)
                continue
            # the body's condition outranks the cosmetic moods: busy is
            # decoration, hurting is information
            if cond == "hurts":
                hurting(ring)
                continue
            if cond == "mute":
                muted(ring)
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