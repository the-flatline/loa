"""oled — the face daemon (loa-oled). Owns SPI0.

Polls the cortex state row (oled_mode / oled_text / oled_dim) and renders
the mode into the SH1106 framebuffer. Same decoupled pattern as the ring:
the API never touches hardware, the daemon never thinks about intent.

Modes: scope (the flatline) | ecg | ripple | noise | text (marquee) | off

Runs on the body. The TUI does NOT: `ripperdoc` talks to the cortex API over
HTTP and can run anywhere with the CPU for it — run it on dixie against the
Pi (`LOA_API_BIND=192.168.1.200`), never on the Pi itself. It is a
full-screen redraw loop; on an uncooled board it is a heater with a UI.
"""

import random
import time

from . import cortex
from . import fault
from . import face

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


def _ring_is_dark() -> bool:
    """True when nothing is driving the ring bus — the sweep names this
    RING DARK."""
    rep = fault.status()
    return any(r.get("code") == "RING DARK" for r in (rep.get("rows") or []))


def _body_condition(ttl=2.0) -> str:
    """The body's own condition, polled slowly — this sits inside the 30fps
    render loop. A broken sweep reads as mute, never as well."""
    now = time.time()
    if now - _BODY["ts"] < ttl:
        return _BODY["val"]
    try:
        val = fault.condition()
    except Exception:                                       # noqa: BLE001
        val = "mute"
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

# the face's topic on the loa frame bus — RAM-backed, mirror of the panel
OLED_TOPIC = "/dev/shm/loa-oled.bin"

_LAST_FACE: dict = {"buf": None}

BRIGHT = 0xCF
DIM = 0x18

# pixel wash: full-frame static every few minutes keeps the panel's pixels
# from holding one pattern long enough to ghost (Divv's suggestion).
WASH_EVERY_S = 300.0
WASH_SECS = 5.0


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
            st = cortex.get_state()
            # The face is the deliberate tell: when the body hurts it names
            # where. It takes over an IDLE face — at the bench you are driving
            # the pages and it must not fight you for them — but if the RING
            # IS DARK it takes over regardless: when the eyes are out, the
            # mouth has to speak or the body has no channel left at all.
            if _body_condition() in ("hurts", "mute"):
                if st["oled_mode"] != "ripperdoc" or _ring_is_dark():
                    st = {**st, "oled_mode": "ripperdoc",
                          "ripperdoc_page": "fault"}
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
                cortex.log_event("wash", {"secs": WASH_SECS})
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
    """Publish the framebuffer to /dev/shm/loa-oled.bin (1KB) — on CHANGE only.

    The daemon redraws 4-30x a second, but a status page is usually identical
    frame to frame. Rewriting identical bytes is work with no reader benefit,
    and the file's mtime is what tells a consumer the frame moved. Publish-on-
    change is also exactly the semantics a subscriber needs, so this is the
    first half of the pub/sub bridge done in a way that cannot break anything.
    """
    buf = bytes(frame.buf)
    if _LAST_FACE["buf"] == buf:
        return
    try:
        with open(OLED_TOPIC, "wb") as f:
            f.write(buf)
    except OSError:
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
    cortex.log_event("boot", {"svc": "oled"})
    render_loop()


if __name__ == "__main__":
    main()