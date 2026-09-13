"""ring — the ring display (loa-ring). Owns the WS2812B over SPI1.

A PURE DISPLAY. It subscribes to the `ring` topic and blits the 72 bytes the
cortex rendered (24 px RGB). It does not render, does not derive state, and
pushes nothing up.

The tell — alarm > hurts > mute > busy > one-shot > home — used to run in a
60fps loop in this file, deciding its own colour from a mirror of the feed and
sending the result back UP to the cortex. That is a limb deciding how the body
feels and then informing the brain. It lives in `loa/frames.py` now, where the
cortex can see it, and the cortex publishes the pixels on the same tick as
everything else.

The ring is still the body's INVOLUNTARY tell — the ears and the tail — and it
still works when the face cannot; that is a property of the tell, not of this
process.
"""
import time

from . import animations as anim
from . import frames
from . import topic as topic_mod
from .ws2812 import Ring


def _pixels(raw: bytes):
    """72 raw RGB bytes -> 24 (r, g, b) tuples, in wire order."""
    return [tuple(raw[i:i + 3]) for i in range(0, len(raw), 3)]


def main(ring=None):
    ring = ring or Ring(num=anim.LED_COUNT)
    mirror = topic_mod.Mirror(topics=["ring"])
    last = {"buf": None}
    try:
        # The SUB is fresh: wait before judging a silent feed.
        time.sleep(0.3)
        while True:
            msg = mirror.message("ring")
            if msg is not None:
                raw = bytes(msg.ring)
                if len(raw) == frames.RING_BYTES and raw != last["buf"]:
                    last["buf"] = raw
                    ring.show(_pixels(raw))
            time.sleep(0.02)
    finally:
        mirror.close()
        ring.close()


if __name__ == "__main__":
    main()
