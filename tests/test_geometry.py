"""One number, two languages.

The glass and the strip are Rust now (rust/loa-panel, rust/loa-ring) and the
wire that describes them is Python. So the sizes they must agree about — 1024
bytes of face, 72 bytes of ring — are written down TWICE, in two languages, with
no compiler that can see both.

That is exactly the shape that produced the framing bug on 2026-09-14: two
implementations of one rule, each internally consistent, disagreeing at the
seam. So the seam is tested: the constants in the Rust sources are read here and
compared against `loa/geom.py`, the one place the numbers are defined.

This replaced tests/test_display_boundary.py, whose subject was the Python
displays' import graph. Those modules are deleted; the boundary they asserted
was one language, and the one that needs testing now is the cross-language one.
"""
import pathlib
import re

from loa import geom
from loa.cortex import frames, ring

RUST = pathlib.Path(__file__).resolve().parent.parent / "rust"


def _const(path, name):
    """The integer value of `pub const NAME: ... = 128;` in a Rust source."""
    text = path.read_text()
    m = re.search(r"pub const %s:\s*usize\s*=\s*([0-9_]+)\s*;" % name, text)
    assert m, "no `pub const %s` in %s" % (name, path)
    return int(m.group(1).replace("_", ""))


def test_the_rust_panel_agrees_with_the_python_geometry():
    """The panel's frame size is the wire's frame size. A Rust panel that
    disagreed would blit a wrong-shaped frame onto the wrong pixels, and the
    Python side would have no way to notice."""
    src = RUST / "loa-panel" / "src" / "panel.rs"
    assert _const(src, "WIDTH") == geom.FACE_WIDTH
    assert _const(src, "PAGES") == geom.FACE_PAGES
    assert _const(src, "HEIGHT") == geom.FACE_HEIGHT
    # BYTES is COMPUTED in panel.rs (WIDTH * PAGES), on purpose — so the check
    # here is the product, not a literal that could disagree with its own parts.
    assert _const(src, "WIDTH") * _const(src, "PAGES") == geom.FACE_BYTES


def test_the_rust_ring_agrees_with_the_python_geometry():
    src = RUST / "loa-ring" / "src" / "neopixel.rs"
    assert _const(src, "LEDS") == geom.RING_LEDS
    assert _const(src, "LEDS") * 3 == geom.RING_BYTES


def test_the_renderers_still_emit_exactly_what_the_wire_carries():
    assert geom.FACE_BYTES == 1024 and geom.RING_BYTES == 72
    assert frames.FACE_BYTES == geom.FACE_BYTES
    assert frames.RING_BYTES == geom.RING_BYTES
    assert ring.anim.LED_COUNT == geom.RING_LEDS


def test_the_sizes_are_computed_not_spelled_out():
    """A number written twice drifts; a number computed from the one definition
    cannot. This catches the case where someone replaces 128*8 with 1024."""
    src = (RUST / "loa-panel" / "src" / "panel.rs").read_text()
    assert "pub const BYTES: usize = WIDTH * PAGES;" in src
    ring_src = (RUST / "loa-ring" / "src" / "neopixel.rs").read_text()
    assert "pub const BYTES: usize = LEDS * 3;" in ring_src
    assert geom.FACE_BYTES == geom.FACE_WIDTH * geom.FACE_PAGES
    assert geom.RING_BYTES == geom.RING_LEDS * 3
