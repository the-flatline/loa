"""frames — state in, pixels out. The brain's picture ASSEMBLY.

The renders that used to live inside the device daemons: `oled` painted the face
and `ring` ran the tell, each deriving its own state and pushing the result back
UP to the cortex. That was a limb informing the brain. The cortex already holds
the state and already imports the pure renderers, so the picture is rendered
HERE, from the state the cortex owns, and published on the `ripperdoc`/`ring`
topics. A daemon blits what it is TOLD.

This module ASSEMBLES: the two byte counts, the mode table, the wash and the
FaceRenderer. What the state MEANS it reads from `loa/cortex/ring.py`, which owns the
ring's builder and the condition vocabulary both renderers share. The face's
pages are `loa/cortex/face.py`; the ring's builder is `loa/cortex/ring.py` —
both in the brain, beside this.

The rules this module keeps, by construction:

  * PURE. No hardware, no /dev/shm, no store, no other service. A renderer that
    reaches for a file or a db is the cross-service read this design removes.
  * The bytes are the whole state. `FaceRenderer.render(st)` returns exactly
    1024 bytes (128x64, 1bpp, page-major) and `RingRenderer.render(st)` exactly
    72 (24 px RGB) — the same numbers the wire carries, so the glass and the
    feed cannot disagree.
  * The TELL (alarm > hurts > mute > busy > one-shot > home) is decided in the
    brain, in one place, rather than on a daemon that had to be running to say
    it.
"""
import random
import time

from .. import geom
from . import face
from .ring import condition, ring_dark

#: The two invariants, imported from `geom` rather than recomputed here: the
#: wire, the panel and the renderer all read the SAME number, so a renderer and
#: a display cannot drift apart by arithmetic done twice.
FACE_BYTES = geom.FACE_BYTES               # 1024: 128x64, 1bpp, page-major
RING_BYTES = geom.RING_BYTES               # 72: 24 px RGB

#: Panel contrast. A DISPLAY characteristic: the daemon applies it, the cortex
#: says which. (See oled/__main__.py — the panel's flip is the same kind of thing.)
BRIGHT = 0xCF
DIM = 0x18

#: Pixel wash: a full-frame exercise every few minutes so no pattern holds long
#: enough to ghost (Divv's suggestion). It is rendered HERE, like everything
#: else, so the glass still shows only what the cortex published — a wash the
#: daemon drew itself would be a second drawer and a second picture.
WASH_EVERY_S = 300.0
WASH_SECS = 5.0

MODE_CLASSES = {
    "scope": face.Scope,
    "ecg": face.ECG,
    "ripple": face.Ripple,
    "noise": face.Noise,
    "text": lambda text=None: face.Marquee(text or "LOA"),
    "showoff": face.Showoff,
    "ripperdoc": face.Ripperdoc,
}


def _make_renderer(mode, state):
    """The object that draws a mode. Stateful modes (scope, showoff, ripperdoc)
    keep their own clocks; they are held by the FaceRenderer, not rebuilt per
    frame."""
    if mode == "off":
        return None
    cls = MODE_CLASSES.get(mode)
    if cls is None:
        return face.Marquee(f"?? {mode}")
    if mode == "text":
        return cls(state.get("oled_text", "LOA"))
    return cls()


def face_state(st) -> dict:
    """The face's view of the state.

    The face is the deliberate tell: when the body hurts (or the sweep is dead
    and the body cannot speak at all) the face names where. It takes over an
    IDLE face — at the bench you are driving the pages and it must not fight
    you — but if the RING IS DARK it takes over regardless: when the eyes are
    out, the mouth has to speak or the body has no channel left.
    """
    st = dict(st)
    if condition(st) in ("hurts", "mute") or ring_dark(st):
        if st.get("oled_mode") != "ripperdoc" or ring_dark(st):
            st["oled_mode"] = "ripperdoc"
            st["page"] = "fault"
    return st



class FaceRenderer:
    """State -> 1024 bytes. Holds the animation objects and the wash clock.

    ONE instance lives in the cortex: the renderers carry their own clocks and
    a Scope's blips are their own memory, so they must not be rebuilt per tick.
    """

    def __init__(self):
        self.frame = face.Frame()
        self.renderer = None
        self.key = None
        self.last_t = time.time()
        self.next_wash = time.time() + WASH_EVERY_S
        self.wash_until = 0.0
        self.wash_blink = 0
        self.rng = random.Random()

    def render(self, st, t=None) -> bytes:
        t = time.time() if t is None else t
        st = face_state(st)
        dt, self.last_t = t - self.last_t, t
        mode = st.get("oled_mode") or "scope"

        # the wash: exercise every pixel for WASH_SECS, every WASH_EVERY_S.
        # Skips "off" — a blank face is not forming retention.
        if mode != "off" and t >= self.next_wash:
            self.wash_until = t + WASH_SECS
            self.next_wash = t + WASH_EVERY_S
            self.wash_blink = 0
        if t < self.wash_until:
            return self._wash(t)

        key = (mode, st.get("oled_text"))
        if key != self.key:
            self.key = key
            self.renderer = _make_renderer(mode, st)
        self.frame.clear()
        if self.renderer is not None:
            if hasattr(self.renderer, "tick"):
                self.renderer.tick(dt)
            if hasattr(self.renderer, "draw_state"):
                self.renderer.draw_state(self.frame, t, st)
            else:
                self.renderer.draw(self.frame, t)
        return bytes(self.frame.buf)

    def _wash(self, t) -> bytes:
        """Full-field blink, then dense static. Every pixel driven, and driven
        THROUGH the topic like every other frame."""
        buf = self.frame.buf
        if t - (self.wash_until - WASH_SECS) < 2.0:
            buf[:] = (b"\xff" if self.wash_blink % 2 == 0 else b"\x00") * FACE_BYTES
            self.wash_blink += 1
        else:
            for i in range(FACE_BYTES):
                buf[i] = self.rng.randrange(256)
        return bytes(buf)
