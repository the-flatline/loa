"""ring — the ring's frame builder: THE RING'S TELL.

State -> 72 bytes. This is what the ring SHOWS, decided in the BRAIN from the
state the cortex already holds — because the cortex knew what it had told the
OLED to render, and a limb that decides its own colour is a limb informing the
brain. `loa/ring/` holds only the LEDs: the hardware (`neopixel.py`), the code
space (`encode.py`), the voice (`animations.py`) and the blit loop
(`__main__.py`).

The tell, highest first: alarm > hurts > mute > busy > one-shot > home. The
condition vocabulary (`condition`, `ring_dark`) lives here because the ring is
the channel that carries the news when the face is dark: a dead face is a black
rectangle, and that black rectangle IS the signal.

Error diffusion is per-instance and reset when the tell CHANGES, so no fractional
carryover flashes on a transition. Nothing here touches hardware or the store.
"""
import time

from ..ring import animations as anim
from ..ring.encode import DitheredFrame

#: The animation clock. Rendering runs at the topic tick (2Hz) now, so this is
#: only used to sample the time-based sequences.
FPS = 30
FRAME_PERIOD = 1.0 / FPS

NIGGLE_PERIOD_S = 20.0


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
