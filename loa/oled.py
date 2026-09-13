"""oled — the face display (loa-oled). Owns SPI0.

A PURE DISPLAY. It subscribes to the `ripperdoc` topic and blits the 1024 bytes
the cortex rendered. It does not render, does not derive state, and pushes
nothing up: the cortex already holds the state and already imports the pure
renderer, so the BRAIN owns the picture. A display that paints its own frame is
a limb that moves on its own and then tells the brain what it did.

AND IT CANNOT REACH THE RENDERER. Its imports are exactly the DRIVER
(loa/panel.py), the frame GEOMETRY (loa/geom.py) and the TOPIC (loa/topic.py):
this process cannot import loa/face.py or the state derivation that lives in it
(seal_state, faults_state, power_status), and it cannot reach loa/frames.py or
loa/cortex.py either. That is a structural boundary, not a promise —
tests/test_display_boundary.py asserts it on the import graph.

What stays here is exactly what the brain cannot do, and both are properties of
the PANEL rather than of the picture:

  * ORIENTATION (`oled_flip`). Applied on the panel (segment remap + COM scan),
    never to the bytes: the frame is also what the feed publishes, so a
    byte-level flip turns the glass right way up and leaves every consumer
    drawing the face upside down. The image on the wire stays canonical.
  * CONTRAST (`oled_dim`). The SH1106's own contrast register — a display
    characteristic, and a per-panel one. The cortex knows the setting and puts
    it on the topic; the panel applies it.

Both ride the ripperdoc message, so no daemon has to read the state to find out
what it is showing. Renderers live in loa/frames.py, and the two bytes counts
(1024 face, 72 ring) are the same on the wire and on the glass because they are
one number: loa/geom.py.
"""
import time

from . import geom
from . import panel as panel_mod
from . import topic as topic_mod

#: The orientation the panel has been told to use. The panel cannot be read
#: back, so this is the only record of it.
_FLIP = {"on": None}
#: The last bytes blitted, and the last contrast set. Re-blitting identical
#: bytes is work no one can see; this is not state derivation, only the display
#: declining to draw the same picture twice.
_LAST = {"buf": None, "contrast": None}

BRIGHT = 0xCF
DIM = 0x18


def _blit(display, msg) -> bool:
    """Draw one ripperdoc message. Returns True when the panel was written.

    The ONLY thing that reaches the glass: the bytes from the topic.
    """
    raw = bytes(msg.face)
    if len(raw) != geom.FACE_BYTES:
        return False            # a truncated frame is not a frame
    flip = bool(msg.oled_flip) if msg.HasField("oled_flip") else False
    contrast = DIM if msg.oled_dim else BRIGHT
    if (raw == _LAST["buf"] and contrast == _LAST["contrast"]
            and flip == _FLIP["on"]):
        return False
    if flip != _FLIP["on"]:
        _FLIP["on"] = flip
        display.set_flip(flip)
    if contrast != _LAST["contrast"]:
        _LAST["contrast"] = contrast
        display.set_contrast(contrast)
    _LAST["buf"] = raw
    display.show(bytearray(raw))
    return True


def main():
    display = panel_mod.get_display()
    mirror = topic_mod.Mirror(topics=["ripperdoc"])
    seen = {"id": None}
    try:
        # A panel must never sit on the last process's frozen frame.
        display.clear()
        # Let the SUB establish before judging the feed silent.
        time.sleep(0.3)
        while True:
            msg = mirror.message("ripperdoc")
            if msg is not None and id(msg) != seen["id"]:
                seen["id"] = id(msg)
                _blit(display, msg)
            time.sleep(0.05)
    finally:
        mirror.close()
        try:
            display.close()
        except Exception:                                   # noqa: BLE001
            pass


if __name__ == "__main__":
    main()
