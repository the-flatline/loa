"""frames — state in, pixels out. The brain's picture.

The renders that used to live inside the device daemons: `oled.py` painted the
face and `ring.py` ran the tell, each deriving its own state and pushing the
result back UP to the cortex. That was a limb informing the brain. The cortex
already holds the state and already imports the pure renderers, so the picture
is rendered HERE, from the state the cortex owns, and published on the
`ripperdoc`/`ring` topics. A daemon blits what it is TOLD.

The rules this module keeps, by construction:

  * PURE. No hardware, no /dev/shm, no store, no other service. A renderer that
    reaches for a file or a db is the cross-service read this design removes.
  * The bytes are the whole state. `FaceRenderer.render(st)` returns exactly
    1024 bytes (128x64, 1bpp, page-major) and `RingRenderer.render(st)` exactly
    72 (24 px RGB) — the same numbers the wire carries, so the glass and the
    feed cannot disagree.
  * The TELL (alarm > hurts > mute > busy > one-shot > home) lives here now, in
    one place, rather than on a daemon that had to be running to say it.
"""
import random
import time

from . import animations as anim
from . import face
from .render import DitheredFrame

#: The two invariants. The wire and the panel agree because these are the only
#: sizes either side produces.
FACE_BYTES = face.WIDTH * face.PAGES       # 1024: 128x64, 1bpp, page-major
RING_BYTES = anim.LED_COUNT * 3            # 72: 24 px RGB

#: The animation clock. Rendering runs at the topic tick (2Hz) now, so this is
#: only used to sample the time-based sequences.
FPS = 30
FRAME_PERIOD = 1.0 / FPS

#: Panel contrast. A DISPLAY characteristic: the daemon applies it, the cortex
#: says which. (See oled.py — the panel's flip is the same kind of thing.)
BRIGHT = 0xCF
DIM = 0x18

#: Pixel wash: a full-frame exercise every few minutes so no pattern holds long
#: enough to ghost (Divv's suggestion). It is rendered HERE, like everything
#: else, so the glass still shows only what the cortex published — a wash the
#: daemon drew itself would be a second drawer and a second picture.
WASH_EVERY_S = 300.0
WASH_SECS = 5.0

NIGGLE_PERIOD_S = 20.0

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


# --------------------------------------------------------------------------- #
# what the state MEANS — read here, once, by both renderers
# --------------------------------------------------------------------------- #

def condition(st) -> str:
    """The body's condition, from the cortex's own state.

    A fault report that has never been heard reads as MUTE, never as well —
    silence is not health. `fault_ts` is the record that a sweep arrived; the
    init handshake is what restores it after a cortex restart.
    """
    if not st.get("fault_ts"):
        return "mute"
    return st.get("condition") or "well"


def _row_codes(st):
    return {r.get("code") for r in (st.get("faults") or []) if isinstance(r, dict)}


def ring_dark(st) -> bool:
    """True when nothing is driving the ring bus — the sweep names this
    RING DARK."""
    return "RING DARK" in _row_codes(st)


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


# --------------------------------------------------------------------------- #
# the face
# --------------------------------------------------------------------------- #

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


# --------------------------------------------------------------------------- #
# the ring — the body's involuntary tell
# --------------------------------------------------------------------------- #

def _solid(h: float, s: float, v: float):
    """One colour across the whole ring."""
    return [anim.hsv(h, s, v)] * anim.LED_COUNT


#: hurts: the breath STOPS and holds a deep red pulse — (bright x3, dim x4),
#: 0.28s a step. Deliberately not the alarm triple-flash: alarm says 'look at
#: me NOW', this says 'something is wrong inside me'.
_HURTS = (0.85, 0.85, 0.85, 0.18, 0.18, 0.18, 0.18)
_HURTS_S = 0.28 * len(_HURTS)

#: alarm: three red pulses with gaps, then a rest — 0.4s on, 0.3s off, and a
#: 2.0s hold at the end of the cycle.
_ALARM_S = 3 * 0.7 + 2.0

#: mute: dark, one dim blink every 10s. Alive but cannot speak — without the
#: blink a deaf body and an unplugged cable look identical.
_MUTE_S = 10.0
_BLINK_S = 0.4

_HOME_BREATH = anim.breath_frames(peak=anim.HOME_PEAK, fps=FPS)
_HOME_S = 3.0 + 0.8 + 4.2 + 0.5
_BUSY_BREATH = anim.busy_frames(fps=FPS)
_BUSY_S = 2.4 + 0.5 + 3.4 + 0.4
_GLITCH = anim.glitch_frames(fps=FPS)
_GLITCH_S = 0.3


def _sample(seq, t, total_s):
    """The frame of a time-based sequence at time t (wraps)."""
    i = int((t % total_s) / total_s * len(seq))
    return seq[min(i, len(seq) - 1)]


class RingRenderer:
    """State -> 72 bytes, and the oneshot memory the tell needs.

    One instance lives in the cortex. The error-diffusion accumulators persist
    across ticks (that is what makes a 2Hz ring dither smoothly), and are reset
    when the tell CHANGES so no fractional carryover flashes on a transition.
    """

    def __init__(self):
        self.dither = DitheredFrame(anim.LED_COUNT)
        self.tell = None
        self.oneshot = None        # (kind, started_at) — a RECORD, played once
        self.niggle_next = 0.0
        self.niggle_until = 0.0

    def note_oneshot(self, kind, ts):
        """A scan/glitch as a RECORD, not a flag: play the newest one the
        cortex has not played yet. Nothing has to be cleared, because nothing
        is a flag — that is what silently ate a second one-shot before."""
        if kind in ("scan", "glitch"):
            self.oneshot = (str(kind), float(ts))

    def render(self, st, t=None) -> bytes:
        t = time.time() if t is None else t
        cond = condition(st)
        rs = st.get("ring_state") or "home"
        kind, frame = None, None
        if rs == "alarm":
            kind, frame = "alarm", self._alarm(t)
        elif cond == "hurts":
            kind, frame = "hurts", self._hurts(t)
        elif cond == "mute":
            kind, frame = "mute", self._mute(t)
        elif rs == "busy":
            kind, frame = "busy", self._busy(t)
        else:
            one = self._oneshot(t, st)
            if one is not None:
                kind, frame = one
            else:
                kind, frame = "home", self._home(t, cond)
        if kind != self.tell:
            self.tell = kind
            self.dither = DitheredFrame(anim.LED_COUNT)
        return self._blit(frame)

    # -- the tells -------------------------------------------------------- #

    def _alarm(self, t):
        p = t % _ALARM_S
        if p >= 3 * 0.7:                       # the rest at the end
            return _solid(0.0, 0.0, 0.0)
        return _solid(0.0, 1.0, 1.0) if (p % 0.7) < 0.4 else _solid(0.0, 0.0, 0.0)

    def _hurts(self, t):
        i = int((t % _HURTS_S) / 0.28)
        return _solid(0.0, 1.0, _HURTS[min(i, len(_HURTS) - 1)])

    def _mute(self, t):
        return _solid(0.0, 0.0, 0.06 if (t % _MUTE_S) >= (_MUTE_S - _BLINK_S)
                      else 0.0)

    def _busy(self, t):
        return _sample(_BUSY_BREATH, t, _BUSY_S)

    def _home(self, t, cond):
        # a niggle rides the breath rather than replacing it — one amber tick,
        # rare enough to be information instead of noise
        if cond == "niggle":
            if t >= self.niggle_next:
                self.niggle_next = t + NIGGLE_PERIOD_S
                self.niggle_until = t + 0.36
            if t < self.niggle_until:
                v = 0.55 if t < (self.niggle_until - 0.18) else 0.10
                return _solid(38.0, 1.0, v)
        breath = _sample(_HOME_BREATH, t, _HOME_S)
        hue = anim.home_hue(t)
        return [anim.hsv(hue, 1.0, max(p) / 255) for p in breath]

    def _oneshot(self, t, st):
        if self.oneshot is None or (st.get("ring_state") or "home") != "home":
            return None
        kind, start = self.oneshot
        elapsed = t - start
        if kind == "glitch":
            if elapsed > _GLITCH_S:
                self.oneshot = None
                return None
            seq = _GLITCH
            total = _GLITCH_S
        else:
            lap, fade = (0.9, 0.3) if st.get("ripperdoc") else (1.6, 0.5)
            total = lap + fade
            if elapsed > total:
                self.oneshot = None
                return None
            seq = anim.scan_frames(lap_s=lap, fade_s=fade, fps=FPS)
        return kind, _sample(seq, elapsed, total)

    # -- the wire --------------------------------------------------------- #

    def _blit(self, frame) -> bytes:
        """The dither, then the wire shape: code-space ints, 24 x RGB = 72 B."""
        raw = bytearray()
        for i, (r, g, b) in enumerate(frame):
            for ch, val in enumerate((r, g, b)):
                self.dither.acc[i][ch] += val
                emitted = int(self.dither.acc[i][ch])
                self.dither.acc[i][ch] -= emitted
                raw.append(max(0, min(255, emitted)))
        return bytes(raw)
