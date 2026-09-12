"""faults — the body's own pain sense.

Off the Pi every check degrades to "cannot read" instead of raising: the sweep
must be safe to run anywhere (tests, dixie) and must never invent a fault it
cannot actually see.
"""
from loa import faults


def test_sweep_shape():
    rep = faults.sweep()
    assert isinstance(rep, dict)
    assert {"ts", "rows", "faults", "warns"} <= set(rep)
    assert rep["faults"] + rep["warns"] == len(rep["rows"])


def test_codes_fit_the_face():
    """The FAULT page draws 14 characters. A code that overflows is a fault
    nobody can read."""
    for r in faults.sweep()["rows"]:
        assert r["level"] in ("fault", "warn")
        assert r["code"], "a fault with no name is useless"
        assert len(r["code"]) <= 14, f"{r['code']!r} will not fit the face"
        assert r["text"], "a fault with no detail is a mystery"


def test_status_is_empty_not_an_error_when_never_swept():
    st = faults.status()
    assert isinstance(st, dict)


def test_quiet_format_prints_only_faults():
    rep = {"ts": 0.0, "boot": "test", "faults": 1, "warns": 1, "rows": [
        {"level": "fault", "code": "OLED-DEAD", "text": "nothing holds it"},
        {"level": "warn", "code": "NIGGLE", "text": "minor"},
    ]}
    out = faults.format_report(rep, quiet=True)
    assert "OLED-DEAD" in out
    assert "minor" not in out
