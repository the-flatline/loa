"""geom — the frame geometry. The numbers the wire and the glass share.

ONE definition per number, because these are the invariants a renderer and a
display cannot be allowed to disagree about: the face frame is 1024 bytes
(128x64, 1 bpp, page-major) on the wire AND on the panel, and the ring frame
is 72 (24 px RGB) on both. A renderer that emits a different size, or a
display that accepts one, is the failure this module makes impossible: there
is only one number, so the two sides cannot drift.

Deliberately dependency-free. A display daemon may import its DRIVER, this
GEOMETRY and the TOPIC, and nothing else — reaching for the renderer fails
CI (tests/test_display_boundary.py).
"""

FACE_WIDTH = 128
FACE_HEIGHT = 64
FACE_PAGES = 8
FACE_BYTES = FACE_WIDTH * FACE_PAGES        # 1024: 128x64, 1bpp, page-major

RING_LEDS = 24
RING_BYTES = RING_LEDS * 3                  # 72: 24 px RGB
