"""oled_daemon — the face daemon (loa-oled). Owns SPI0.

Polls the cortex state row (oled_mode / oled_text / oled_dim) and renders
the mode into the SH1106 framebuffer. Same decoupled pattern as the ring:
the API never touches hardware, the daemon never thinks about intent.

Modes: scope (the flatline) | ecg | ripple | noise | text (marquee) | off
"""

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
}

BRIGHT = 0xCF
DIM = 0x18


def render_loop(display=None, max_frames=None):
    """Run until stopped. max_frames is for tests — renders N frames, exits."""
    display = display or oled.get_display()
    frame = oled.Frame()
    last_key = None
    renderer = None
    last_t = time.time()
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
            if renderer is not None:
                now = time.time()
                dt = now - last_t
                last_t = now
                if hasattr(renderer, "tick"):
                    renderer.tick(dt)
                frame.clear()
                renderer.draw(frame, now)
                frame.blit(display)
            frames += 1
            time.sleep(FRAME_PERIOD)
    finally:
        try:
            display.close()
        except Exception:
            pass


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