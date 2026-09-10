"""oled_daemon — the face daemon (loa-oled). Owns SPI0.

Polls the cortex state row (oled_mode / oled_text / oled_dim) and renders
the mode into the SH1106 framebuffer. Same decoupled pattern as the ring:
the API never touches hardware, the daemon never thinks about intent.

Modes: scope (the flatline) | ecg | ripple | noise | text (marquee) | off
"""

import random
import time

from . import cortex
from . import oled

FPS = 30
FRAME_PERIOD = 1.0 / FPS

MODE_CLASSES = {
    "scope": oled.Scope,
    "ecg": oled.ECG,
    "ripple": oled.Ripple,
    "noise": oled.Noise,
    "text": lambda text=None: oled.Marquee(text or "LOA"),
    "showoff": oled.Showoff,
    "ripperdoc": oled.Ripperdoc,
}

# the face's topic on the loa frame bus — RAM-backed, mirror of the panel
OLED_TOPIC = "/dev/shm/loa-oled.bin"

BRIGHT = 0xCF
DIM = 0x18

# pixel wash: full-frame static every few minutes keeps the panel's pixels
# from holding one pattern long enough to ghost (Divv's suggestion).
WASH_EVERY_S = 300.0
WASH_SECS = 5.0


def render_loop(display=None, max_frames=None):
    """Run until stopped. max_frames is for tests — renders N frames, exits."""
    display = display or oled.get_display()
    frame = oled.Frame()
    last_key = None
    renderer = None
    last_t = time.time()
    last_wash = time.time()
    frames = 0
    try:
        display.clear()
        while max_frames is None or frames < max_frames:
            st = cortex.get_state()
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
            time.sleep(FRAME_PERIOD)
    finally:
        try:
            display.close()
        except Exception:
            pass


def _publish_face(frame) -> None:
    """Publish the exact framebuffer to /dev/shm/loa-oled.bin (1KB).

    The face's topic on the loa frame bus — RAM-backed, same model as the
    ring. Web/relay consumers read it via the API, never touch loa directly.
    """
    try:
        with open(OLED_TOPIC, "wb") as f:
            f.write(frame.buf)
    except OSError:
        pass


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
            frame.buf[:] = b"\xff" * (oled.WIDTH * oled.PAGES)  # every px ON
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
        return oled.Marquee(f"?? {mode}")
    if mode == "text":
        return cls(state.get("oled_text", "LOA"))
    return cls()


def main():
    cortex.log_event("boot", {"svc": "oled"})
    render_loop()


if __name__ == "__main__":
    main()