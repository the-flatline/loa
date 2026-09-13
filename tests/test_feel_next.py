"""`feel {next}` — the mood vocabulary belongs to the cortex, not the console.

A console with its own copy of the mood list is a second copy of a vocabulary,
and two copies drift. So the console says "advance" and the cortex decides what
comes next. The Rust console had exactly this gap: its 'm' key could only ever
send one hardcoded mood, and the Python console (which cycles a local list) was
about to be deleted underneath it.
"""
import loa.cortex.moods as moods
from loa.cortex import state as cortex


def _call_feel(monkeypatch, args):
    """Run the door's feel verb against a state of our own."""
    from loa.cortex import __main__ as cortexd
    monkeypatch.setattr(cortexd.moods, "apply_ring", lambda *a, **k: None)
    return cortexd._v_feel(args, token=None)


def test_next_advances_from_the_mood_the_body_is_actually_in(monkeypatch):
    names = list(moods.MOODS)
    cortex.set_state({"mood": names[0]})
    got = _call_feel(monkeypatch, {"next": True})
    assert got["feeling"] == names[1], "next did not advance one step"

    cortex.set_state({"mood": names[-1]})
    got = _call_feel(monkeypatch, {"next": True})
    assert got["feeling"] == names[0], "next did not wrap at the end"


def test_next_on_an_unknown_mood_starts_at_the_top(monkeypatch):
    """A mood the vocabulary does not contain has no index to advance from.
    Guessing one would be inventing a position; starting at the top does not."""
    cortex.set_state({"mood": "not-a-mood"})
    got = _call_feel(monkeypatch, {"next": True})
    assert got["feeling"] == list(moods.MOODS)[0]


def test_a_named_feeling_still_works_and_nonsense_is_still_refused(monkeypatch):
    names = list(moods.MOODS)
    assert _call_feel(monkeypatch, {"feeling": names[0]})["feeling"] == names[0]
    try:
        _call_feel(monkeypatch, {"feeling": "not-a-mood"})
    except Exception as e:                                   # HTTPException
        assert "must be one of" in str(e)
    else:                                                    # pragma: no cover
        raise AssertionError("a feeling outside the vocabulary was accepted")
