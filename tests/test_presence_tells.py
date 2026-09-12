"""presence — the ring's wordless tell.

The ring must show how the body is WITHOUT words, and it must keep showing it
when the face is dead. A dead face is a black rectangle and that black
rectangle is the signal, so the ring is the channel that carries the news.

Condition outranks the cosmetic moods: being busy is decoration, hurting is
information. Only an explicit alarm outranks it.
"""
import loa.presence as pres
from loa import animations as anim
from loa import faults


class FakeRing:
    """Records frames instead of driving SPI."""

    def __init__(self):
        self.frames = []

    def show(self, frame):
        self.frames.append(frame)

    def close(self):
        pass


def _set(monkeypatch, cond):
    monkeypatch.setattr(pres, "_condition", lambda ttl=1.0: cond)
    pres._COND["ts"] = 0.0


def test_hurts_stops_breathing_and_holds_red(monkeypatch):
    """The tell is the stopped rhythm, not a colour: a body in pain does not
    keep breathing evenly, and it must not look like the alarm yell."""
    ring = FakeRing()
    calls = {"n": 0}

    def cond(ttl=1.0):
        calls["n"] += 1
        return "hurts" if calls["n"] <= 8 else "well"

    monkeypatch.setattr(pres, "_condition", cond)
    monkeypatch.setattr(pres.time, "sleep", lambda s: None)
    pres.hurting(ring)

    assert ring.frames, "hurting rendered nothing"
    for f in ring.frames:
        assert len(f) == anim.LED_COUNT
        r, g, b = f[0]
        assert r > g and r > b, "hurting must be red, not a mood colour"


def test_mute_blinks_so_silence_does_not_read_as_calm(monkeypatch):
    """A dark ring with no blink is indistinguishable from an unplugged cable.
    Alive-but-mute has to look different from dead."""
    ring = FakeRing()
    calls = {"n": 0}

    def cond(ttl=1.0):
        calls["n"] += 1
        return "mute" if calls["n"] <= 2 else "well"

    monkeypatch.setattr(pres, "_condition", cond)
    monkeypatch.setattr(pres.time, "sleep", lambda s: None)
    pres.muted(ring)

    assert len(ring.frames) >= 2, "mute must blink, not just go dark"
    assert all(c == 0 for c in ring.frames[0][0]), "mute rests dark"
    assert any(c > 0 for c in ring.frames[1][0]), "mute must blink once"


def test_condition_reads_well_when_the_body_is_clean():
    assert faults.condition({"ts": __import__("time").time(), "faults": 0,
                             "warns": 0, "rows": []}) == "well"


def test_condition_is_niggle_on_warnings_only():
    assert faults.condition({"ts": __import__("time").time(), "faults": 0,
                             "warns": 2, "rows": []}) == "niggle"


def test_stale_or_missing_sweep_reads_mute_never_well():
    import time as _t
    assert faults.condition({}) == "mute", "no sweep must not read as calm"
    stale = {"ts": _t.time() - faults.FAULTS_STALE_S - 1,
             "faults": 0, "warns": 0, "rows": []}
    assert faults.condition(stale) == "mute", "a deaf sense is not a well body"
