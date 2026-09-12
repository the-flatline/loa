"""amiga font — baseline placement.

The rasterizer emitted the period flush to the TOP of the cell. On the
ripperdoc PWR page that made "2.358A" read as "2 degrees 358 Amps": the dot
was drawing in rows 0-1, above the digits. A glyph's ink belongs at the
bottom of its cell (digits and capitals are 7 rows in an 8-row cell, so the
baseline is row 6 of the non-descender range).
"""
from loa import amiga


def test_period_sits_on_the_baseline():
    rows = amiga.GLYPHS[8]["."]["r"]
    ink = [i for i, mask in enumerate(rows) if mask]
    assert ink, "period has no ink"
    assert min(ink) >= 5, (
        f"period ink starts at row {min(ink)} — it floats above the digits; "
        "it belongs at the bottom of the cell (baseline row 6)")


def test_glyph_rows_fit_inside_their_cell():
    for size, glyphs in amiga.GLYPHS.items():
        for ch, g in glyphs.items():
            assert len(g["r"]) <= size, (
                f"{ch!r} at size {size} is {len(g['r'])} rows; the cell is {size}")
