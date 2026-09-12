"""oled — the face daemon (loa-oled). Owns SPI0.

SUBSCRIBES to the topics and renders the mode into the SH1106 framebuffer.
It does NOT read the cortex: a daemon reaching into another service's state is
the cross-service read this design removes — and the failure it caused is on
record, a face daemon happily rendering a mood the body had already dropped.
The mode arrives on the ripperdoc topic, and the frames go back up on it.
Same decoupled pattern as the ring: the API never touches hardware, the daemon
never thinks about intent.

Modes: scope (the flatline) | ecg | ripple | noise | text (marquee) | off

Runs on the body. The TUI does NOT: `ripperdoc` talks to the cortex API over
HTTP and can run anywhere with the CPU for it — run it on dixie against the
Pi (`LOA_API_BIND=192.168.1.200`), never on the Pi itself. It is a
full-screen redraw loop; on an uncooled board it is a heater with a UI.
"""

import random
import time

from . import face
from . import topic as topic_mod

#: The feed, as this daemon sees it. The renderers need the mode and the mood;
#: they read them here, never out of the cortex.
_MIRROR = {"m": None}
#: The way back up: the frames this daemon drives, and its own records.
_OUT = {"sock": None}

#: The orientation the panel has been told to use. The panel cannot be read
#: back, so this is the only record of it.
_FLIP = {"on": None}

#: How long a silent feed goes unreported. The tick is 2Hz, so a couple of
#: seconds of nothing is a fault, not a pause.
FEED_STALE_S = 5.0

FPS = 30
# A ripperdoc page is text that changes a few times a second at most. Drawing
# it 30 times a second is pure heat — on an uncooled Pi that is not a metaphor.
# Animation wants frames; a status page wants none. (2026-09-12: 68% CPU in the
# TUI and 11% here had the SoC sitting on its thermal limit.)
IDLE_FPS = 4
FRAME_PERIOD = 1.0 / FPS
IDLE_PERIOD = 1.0 / IDLE_FPS


def _period_for(mode: str) -> float:
    """Frame period for a mode. Only animated modes need the full rate."""
    return IDLE_PERIOD if mode == "ripperdoc" else FRAME_PERIOD

_BODY: dict = {"ts": 0.0, "val": "well"}


def _fault_rows():
    """The body's fault rows, off the fault topic."""
    m = _MIRROR["m"]
    if m is None:
        return []
    msg = m.message("fault")
    return [] if msg is None else list(msg.rows)


def _ring_is_dark() -> bool:
    """True when nothing is driving the ring bus — the sweep names this
    RING DARK."""
    return any(r.code == "RING DARK" for r in _fault_rows())


def _body_condition(ttl=2.0) -> str:
    """The body's own condition, polled slowly — this sits inside the 30fps
    render loop. A broken sweep reads as mute, never as well."""
    now = time.time()
    if now - _BODY["ts"] < ttl:
        return _BODY["val"]
    m = _MIRROR["m"]
    msg = None if m is None else m.message("fault")
    if msg is None:
        # No fault message has EVER arrived — the sweep is not being heard, and
        # that is mute, not well. Reading silence as health is what let a body
        # sit hurting with a calm face.
        val = "mute"
    else:
        val = msg.condition or "well"
    _BODY["ts"], _BODY["val"] = now, val
    return val

MODE_CLASSES = {
    "scope": face.Scope,
    "ecg": face.ECG,
    "ripple": face.Ripple,
    "noise": face.Noise,
    "text": lambda text=None: face.Marquee(text or "LOA"),
    "showoff": face.Showoff,
    "ripperdoc": face.Ripperdoc,
}

_LAST_FACE: dict = {"buf": None}

BRIGHT = 0xCF
DIM = 0x18

# pixel wash: full-frame static every few minutes keeps the panel's pixels
# from holding one pattern long enough to ghost (Divv's suggestion).
WASH_EVERY_S = 300.0
WASH_SECS = 5.0


def _state():
    """The feed, as a flat dict the renderers already understand."""
    m = _MIRROR["m"]
    st = dict(m.state()) if m is not None else {}
    st.setdefault("oled_mode", "scope")
    st.setdefault("oled_text", None)
    st.setdefault("oled_dim", False)
    st.setdefault("page", "sensors")
    st.setdefault("mood", "calm")
    st.setdefault("ring_state", "home")
    st.setdefault("oled_flip", True)
    # A silent feed is REPORTED, never quietly rendered as a healthy body: if
    # nothing has arrived on the ripperdoc topic, the face says so.
    age = None if m is None else m.age("ripperdoc")
    if age is None or age > FEED_STALE_S:
        st["oled_mode"] = "ripperdoc"
        st["page"] = "fault"
    return st


def _log(kind, detail=None):
    """A record. Over the topic — the cortex keeps the store, not this daemon."""
    sock = _OUT["sock"]
    if sock is not None:
        try:
            sock.send("event", topic_mod.event(time.time(), kind, detail))
        except Exception:                                   # noqa: BLE001
            pass


def render_loop(display=None, max_frames=None):
    """Run until stopped. max_frames is for tests — renders N frames, exits."""
    display = display or face.get_display()
    frame = face.Frame()
    last_key = None
    renderer = None
    last_t = time.time()
    last_wash = time.time()
    frames = 0
    try:
        display.clear()
        while max_frames is None or frames < max_frames:
            st = _state()
            # The face is the deliberate tell: when the body hurts it names
            # where. It takes over an IDLE face — at the bench you are driving
            # the pages and it must not fight you for them — but if the RING
            # IS DARK it takes over regardless: when the eyes are out, the
            # mouth has to speak or the body has no channel left at all.
            if _body_condition() in ("hurts", "mute"):
                if st["oled_mode"] != "ripperdoc" or _ring_is_dark():
                    st = {**st, "oled_mode": "ripperdoc", "page": "fault"}
            flip = bool(st.get("oled_flip", True))
            if flip != _FLIP["on"]:
                # The face's orientation, applied on the PANEL: two commands,
                # nothing per frame. Persisted, so an upside-down mount survives
                # a reboot.
                _FLIP["on"] = flip
                display.set_flip(flip)
            key = (st["oled_mode"], st["oled_text"], st["oled_dim"])
            if key != last_key:
                last_key = key
                mode = st["oled_mode"]
                renderer = _make_renderer(mode, st)
                display.set_contrast(DIM if st["oled_dim"] else BRIGHT)
                frame.clear()
                if mode == "off":
                    display.clear()
            # pixel wash: exercise every pixel so no pattern ghosts. Skips
            # "off" — a blank face isn't forming retention.
            if st["oled_mode"] != "off" and time.time() - last_wash >= WASH_EVERY_S:
                _log("wash", {"secs": WASH_SECS})
                display.set_contrast(BRIGHT)      # full swing clears best
                _wash(display, frame, WASH_SECS)
                display.set_contrast(DIM if st["oled_dim"] else BRIGHT)
                last_wash = time.time()
                continue
            if renderer is not None:
                now = time.time()
                dt = now - last_t
                last_t = now
                if hasattr(renderer, "tick"):
                    renderer.tick(dt)
                frame.clear()
                if hasattr(renderer, "draw_state"):
                    renderer.draw_state(frame, now, st)
                else:
                    renderer.draw(frame, now)
                frame.blit(display)
                _publish_face(frame)
            frames += 1
            time.sleep(_period_for(st["oled_mode"]))
    finally:
        try:
            display.close()
        except Exception:
            pass


def _publish_face(frame) -> None:
    """Send the framebuffer up to the cortex, on CHANGE only.

    The daemon redraws 4-30x a second, but a status page is usually identical
    frame to frame. Sending identical bytes is work the cortex would then have
    to publish at the tick anyway, so the change check is here, closest to the
    pixels.

    The frame goes INSIDE a Ripperdoc event — one protobuf message, the raw
    1024 bytes as a field, nothing encoded and nothing beside it.
    """
    sock = _OUT["sock"]
    if sock is None:
        return
    buf = bytes(frame.buf)
    if _LAST_FACE["buf"] == buf:
        return
    try:
        msg = topic_mod.pb.Ripperdoc(face=buf)
        sock.send("ripperdoc", msg)
    except Exception:                                       # noqa: BLE001
        return
    _LAST_FACE["buf"] = buf


def _wash(display, frame, secs):
    """Exercise EVERY pixel, multiple times, provably.

    Phase 1: full-field blink — every pixel fully ON, then fully OFF,
    repeated. Phase 2: dense full-resolution static — every pixel gets a
    random state each frame (about half on, half off). Combined, no pixel
    can be missed: on a 5s wash each pixel is driven fully ~4x in phase 1
    and randomly ~45x on + ~45x off in phase 2.
    """
    # phase 1: full on/off blink at 2Hz (2 full cycles per second)
    blink_end = time.time() + min(2.0, secs * 0.4)
    n = 0
    while time.time() < blink_end:
        frame.clear()
        if n % 2 == 0:
            frame.buf[:] = b"\xff" * (face.WIDTH * face.PAGES)  # every px ON
        frame.blit(display)                                      # odd = all OFF
        time.sleep(0.25)
        n += 1
    # phase 2: dense random static — varied states, total coverage
    rng = random.Random()
    t_end = time.time() + max(0.0, secs - 2.0)
    while time.time() < t_end:
        frame.clear()
        for i in range(len(frame.buf)):
            frame.buf[i] = rng.randrange(256)
        frame.blit(display)
        time.sleep(FRAME_PERIOD)


def _make_renderer(mode, state):
    if mode == "off":
        return None
    cls = MODE_CLASSES.get(mode)
    if cls is None:
        return face.Marquee(f"?? {mode}")
    if mode == "text":
        return cls(state.get("oled_text", "LOA"))
    return cls()


def main():
    _MIRROR["m"] = topic_mod.Mirror(topics=["ripperdoc", "fault", "ring"])
    _OUT["sock"] = topic_mod.Sender()
    # A subscriber will not see a publisher that has only just bound, so the
    # face shows ALL QUIET until the first message lands. That is the honest
    # face for a body whose feed has not started: it says the silence.
    time.sleep(0.3)
    _log("boot", {"svc": "oled"})
    render_loop()


if __name__ == "__main__":
    main()