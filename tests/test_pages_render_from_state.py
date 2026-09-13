"""Pages render the STATE, not this machine.

Found live 2026-09-12: the console on dixie drew every rail as "--" while
/state was carrying the whole PMIC readout. The PWR page called power_status(),
which shells out to vcgencmd — on dixie there is no PMIC, so there were no
numbers, and nothing said so.

That is the third time tonight a local read wore the costume of a remote one:
a local status file, a local cortex database, and now local hardware. Every one
of them produced a plausible empty answer instead of an error.
"""
import pytest

from loa.cortex import face


def _no_local_hardware(monkeypatch):
    """Any attempt to read this machine's hardware is a test failure."""
    def explode():
        raise AssertionError("page read LOCAL hardware instead of the state")
    monkeypatch.setattr(face, "power_status", explode)
    monkeypatch.setattr(face, "faults_state", explode)


def _capture_text(monkeypatch):
    drawn = []
    monkeypatch.setattr(face.amiga, "draw",
                        lambda frame, text, x, y, size=8, **kw: drawn.append(text))
    return drawn


def test_the_power_page_renders_from_state_alone(monkeypatch):
    _no_local_hardware(monkeypatch)
    drawn = _capture_text(monkeypatch)

    st = {"power": {"EXT5V_V": 5.107, "3V3_SYS_V": 3.33, "3V3_SYS_A": 0.127,
                    "VDD_CORE_V": 0.789, "VDD_CORE_A": 1.9, "throttled": 0}}
    face.Ripperdoc()._page_power(face.Frame(), 0.0, st)

    joined = " ".join(drawn)
    assert "5.107V" in joined, f"the 5V input did not render from state: {joined}"
    assert "3.330V" in joined, "the 3V3 rail did not render from state"
    assert "0.127A" in joined, "the rail current did not render"
    assert "--" not in joined, "a rail fell back to dashes with data available"


def test_a_live_undervolt_shows_on_the_page(monkeypatch):
    """UV lit means sagging RIGHT NOW (bit 0). Reading the flags off the state,
    not off a local read the console cannot do.

    Asserting on the drawn label would be worthless — the label draws either
    way. This captures the indicator's actual state."""
    _no_local_hardware(monkeypatch)
    seen = {}
    monkeypatch.setattr(face.Ripperdoc, "_indicator",
                        lambda self, frame, x, y, label, on:
                        seen.__setitem__(label, on))

    st = {"power": {"EXT5V_V": 4.61, "3V3_SYS_V": 3.31, "throttled": 0x1}}
    face.Ripperdoc()._page_power(face.Frame(), 0.0, st)
    assert seen.get("UV") is True, "a live undervolt did not light UV"
    assert seen.get("THR") is False, "THR lit with no throttling bit set"

    # bit 2 is 'throttled right now' — a sagging input that has not yet pushed
    # the SoC into throttling is UV without THR, and the page must say so
    seen.clear()
    st = {"power": {"EXT5V_V": 5.10, "3V3_SYS_V": 3.33, "throttled": 0x4}}
    face.Ripperdoc()._page_power(face.Frame(), 0.0, st)
    assert seen.get("THR") is True and seen.get("UV") is False

    # sticky bits (16+) are 'happened since boot' — not 'happening now'
    seen.clear()
    st = {"power": {"EXT5V_V": 5.10, "3V3_SYS_V": 3.33, "throttled": 0x50000}}
    face.Ripperdoc()._page_power(face.Frame(), 0.0, st)
    assert seen.get("UV") is False and seen.get("THR") is False, (
        "a sticky 'happened since boot' bit must not read as happening now")


def test_with_no_state_it_says_nothing_rather_than_inventing_it(monkeypatch):
    """Off-body with an empty state the page falls back to a local read, finds
    no PMIC, and draws dashes. The honest answer is dashes — never a number
    nobody measured."""
    monkeypatch.setattr(face, "power_status", lambda: {})
    drawn = _capture_text(monkeypatch)
    face.Ripperdoc()._page_power(face.Frame(), 0.0, {})
    joined = " ".join(drawn)
    assert "--" in joined, "an unreadable rail must render as dashes"
