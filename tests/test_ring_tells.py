"""presence — the ring's wordless tell, rendered by the BRAIN.

The tell moved out of the daemon on 2026-09-13. `ring.py` is a pure display now
(it blits the 72 bytes it is told) and what the ring SHOWS is decided in
`loa/frames.py`, from the state the cortex already holds in RAM — because the
cortex knew what it had told the OLED to render, and a limb that decides its own
colour is a limb informing the brain.

The tests keep their meaning, they just ask the renderer instead of driving
hardware: a hurting body's ring STOPS breathing rather than breathing evenly, a
mute sweep still blinks so silence cannot read as calm, and the condition always
outranks the cosmetic mood.

The ring must keep showing how the body is when the face is dead. A dead face is
a black rectangle and that black rectangle is the signal, so the ring is the
channel that carries the news.
"""
import time

from loa import fault, frames


def _px(raw):
    """72 raw bytes -> 24 (r,g,b) triples."""
    return [tuple(raw[i:i + 3]) for i in range(0, len(raw), 3)]


def _st(ring_state="home", condition="well", fault_ts=None):
    return {"ring_state": ring_state, "condition": condition,
            "fault_ts": time.time() if fault_ts is None else fault_ts}


def test_hurts_stops_breathing_and_holds_red():
    """The tell is the stopped rhythm, not just a colour: a body in pain does
    not keep breathing evenly, and it must not look like the alarm yell."""
    r = frames.RingRenderer()
    st = _st(condition="hurts")
    levels = set()
    for i in range(12):
        raw = r.render(st, t=100.0 + i * 0.25)
        for j in range(0, len(raw), 3):
            red, green, blue = raw[j], raw[j + 1], raw[j + 2]
            assert red > green and red > blue, "hurting must be red"
        levels.add(round(max(raw) / 40))
    assert len(levels) <= 2, (
        "hurting must HOLD a pulse (two levels), not sweep through a breath: "
        f"levels {sorted(levels)}")


def test_mute_blinks_so_silence_does_not_read_as_calm():
    """A dark ring with no blink is indistinguishable from an unplugged cable.
    Alive-but-mute has to look different from dead."""
    r = frames.RingRenderer()
    st = _st(condition="mute")
    raws = [r.render(st, t=1000.0 + i * 0.1) for i in range(100)]
    assert max(raws[0]) == 0, "mute rests dark"
    assert any(v > 0 for raw in raws for v in raw), "mute must blink once"


def test_the_condition_outranks_the_cosmetic_mood():
    """Being busy is decoration; hurting is information."""
    busy_well = frames.RingRenderer().render(_st(ring_state="busy"), t=5.0)
    busy_hurts = frames.RingRenderer().render(
        _st(ring_state="busy", condition="hurts"), t=5.0)
    assert bytes(busy_hurts) != bytes(busy_well)
    for red, green, blue in _px(busy_hurts):
        assert red > green and red > blue


def test_a_never_swept_body_is_not_a_calm_ring():
    """Silence is not health: a sweep that has never been heard reads mute, so
    the ring says 'I cannot speak', never the green breath."""
    st = _st(condition="well", fault_ts=None)
    st["fault_ts"] = None               # this body has never heard a sweep
    st["ring_state"] = "home"
    raw = frames.RingRenderer().render(st, t=3.2)
    breath = frames.RingRenderer().render(dict(st, fault_ts=time.time()), t=3.2)
    assert bytes(raw) != bytes(breath), (
        "a never-swept body rendered the well breath — silence read as calm")


def test_condition_reads_well_when_the_body_is_clean():
    assert fault.condition({"ts": time.time(), "faults": 0,
                            "warns": 0, "rows": []}) == "well"


def test_condition_is_niggle_on_warnings_only():
    assert fault.condition({"ts": time.time(), "faults": 0,
                            "warns": 2, "rows": []}) == "niggle"


def test_stale_or_missing_sweep_reads_mute_never_well():
    assert fault.condition({}) == "mute", "no sweep must not read as calm"
    stale = {"ts": time.time() - fault.FAULTS_STALE_S - 1,
             "faults": 0, "warns": 0, "rows": []}
    assert fault.condition(stale) == "mute", "a deaf sense is not a well body"
