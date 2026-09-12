"""presence — the ring daemon (loa-presence). Owns the WS2812B over SPI1.

Theme PHOSPHOR, cortex-driven. Polls the cortex state row every frame and
renders:

  ring_state home   : green breath, cyan drift
  ring_state busy   : amber breath — working
  ring_state alarm  : full red triple pulse — the yell
  ring event scan      : one comet lap — attention, on demand
  ring event glitch    : one RGB-split stutter — corruption, on demand

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
from . import topic as topic_mod

FPS = 60
FRAME_PERIOD = 1.0 / FPS
NIGGLE_PERIOD_S = 20.0

#: The feed as this daemon sees it, and the way back up.
_MIRROR = {"m": None}
_OUT = {"sock": None}
#: The frame sink is rate-limited: the ring redraws at 60fps, the topic ticks at
#: 2Hz. Sending 60 frames a second would flood the cortex's ingest for data the
#: tick will not carry anyway.
SINK_MIN_S = 0.2
_SINK = {"last": None, "at": 0.0}

_CONDS = {"now": None, "until": None}

#: A scan/glitch this daemon has already played. These were a `pending_event`
#: flag in the cortex's state that this daemon CLEARED — a daemon writing another
#: service's state. They are records now: the ring plays the newest ring event it
#: has not played, and nothing has to be cleared because nothing is a flag.
_ONESHOT = {"seen": None}


def _state():
    """The feed, flat, with the defaults a renderer needs to keep drawing."""
    m = _MIRROR["m"]
    st = dict(m.state()) if m is not None else {}
    st.setdefault("ring_state", "home")
    return st


def _log(kind, detail=None):
    sock = _OUT["sock"]
    if sock is not None:
        try:
            sock.send("event", topic_mod.event(time.time(), kind, detail))
        except Exception:                                   # noqa: BLE001
            pass


def _take_oneshot():
    """A scan/glitch, taken once. None when there is nothing new to play."""
    m = _MIRROR["m"]
    if m is None:
        return None
    msg = m.message("event")
    if msg is None or msg.kind != "ring":
        return None
    key = (round(msg.ts, 3), msg.detail.get("state"))
    if key == _ONESHOT["seen"]:
        return None
    _ONESHOT["seen"] = key
    return msg.detail.get("state") or None


def _condition(ttl=1.0) -> str:
    """The body's own report, read at most once a second — this is called every
    frame. A sweep that has never been heard reads as mute, never as well."""
    now = time.monotonic()
    if _CONDS["until"] is not None and now < _CONDS["until"]:
        return _CONDS["now"]
    m = _MIRROR["m"]
    msg = None if m is None else m.message("fault")
    val = "mute" if msg is None else (msg.condition or "well")
    _CONDS["now"], _CONDS["until"] = val, now + ttl
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
        st = _state()
        if st["ring_state"] != "home":
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
    while _state()["ring_state"] == "busy":
        for frame in frames:
            st = _state()
            if st["ring_state"] != "busy":
                return
            dither.render(ring, frame)
            time.sleep(FRAME_PERIOD)


def alarm(ring):
    while _state()["ring_state"] == "alarm":
        for frame in anim.alarm_frames():
            if _state()["ring_state"] != "alarm":
                return
            ring.show(frame)
            time.sleep(0.4 if frame[0][0] > 0 else 0.3)
        time.sleep(2.0)


def one_scan(ring):
    dither = DitheredFrame(anim.LED_COUNT)
    # ripperdoc bench mode: snappier lap so the reaction reads instantly
    st = _state()
    lap, fade = (0.9, 0.3) if st.get("ripperdoc") else (1.6, 0.5)
    for frame in anim.scan_frames(lap_s=lap, fade_s=fade, fps=FPS):
        st = _state()
        if st["ring_state"] != "home":
            return
        dither.render(ring, frame)
        time.sleep(FRAME_PERIOD)


def one_glitch(ring):
    dither = DitheredFrame(anim.LED_COUNT)
    for frame in anim.glitch_frames(fps=FPS):
        st = _state()
        if st["ring_state"] != "home":
            return
        dither.render(ring, frame)
        time.sleep(FRAME_PERIOD)


def _sink(raw):
    """Send the ring's frame up to the cortex — on change, and never faster than
    the topic can carry it."""
    sock = _OUT["sock"]
    if sock is None:
        return
    now = time.monotonic()
    if raw == _SINK["last"] or now - _SINK["at"] < SINK_MIN_S:
        return
    try:
        sock.send("ring", topic_mod.pb.Ring(ring=raw))
    except Exception:                                       # noqa: BLE001
        return
    _SINK["last"], _SINK["at"] = raw, now


def main(ring=None):
    ring = ring or Ring(num=anim.LED_COUNT)
    _MIRROR["m"] = topic_mod.Mirror(topics=["ripperdoc", "fault", "event"])
    _OUT["sock"] = topic_mod.Sender()
    # The driver stays ignorant of the wire: it hands every frame to a sink and
    # this is the sink. (The old path was a file in /dev/shm written behind the
    # publisher's back.)
    ring.frame_sink = _sink
    time.sleep(0.3)          # let the subscriber see the publisher before judging it
    _log("boot", {"svc": "presence"})
    try:
        while True:
            st = _state()
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
            oneshot = _take_oneshot()
            if oneshot == "scan":
                one_scan(ring)
                continue
            if oneshot == "glitch":
                one_glitch(ring)
                continue
            home(ring)
    finally:
        ring.close()


if __name__ == "__main__":
    main()