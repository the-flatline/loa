"""faults — the body's own pain sense.

Off the Pi every check degrades to "cannot read" instead of raising: the sweep
must be safe to run anywhere (tests, dixie) and must never invent a fault it
cannot actually see.
"""
from loa import fault


def test_sweep_shape():
    rep = fault.sweep()
    assert isinstance(rep, dict)
    assert {"ts", "rows", "faults", "warns"} <= set(rep)
    assert rep["faults"] + rep["warns"] == len(rep["rows"])


def test_codes_fit_the_face():
    """The FAULT page draws 14 characters. A code that overflows is a fault
    nobody can read."""
    for r in fault.sweep()["rows"]:
        assert r["level"] in ("fault", "warn")
        assert r["code"], "a fault with no name is useless"
        assert len(r["code"]) <= 14, f"{r['code']!r} will not fit the face"
        assert r["text"], "a fault with no detail is a mystery"


def test_status_is_empty_not_an_error_when_never_swept():
    st = fault.status()
    assert isinstance(st, dict)


def test_every_live_throttle_condition_is_reported():
    """Bits 0-3 are independent. Reporting only one of them is how a thermally
    limited Pi reads as healthy (found live: 84C, throttled=0xf0008)."""
    import loa.fault as f

    rows = []

    class _R:                                             # noqa: D401
        def __init__(self, bits, temp="84.0'C"):
            self.bits = bits
            self.temp = temp

        def __call__(self, cmd, timeout=8):
            if "get_throttled" in cmd:
                return 0, f"throttled=0x{self.bits:x}", ""
            if "measure_temp" in cmd:
                return 0, f"temp={self.temp}", ""
            return 1, "", ""

    orig = f._run
    try:
        f._run = _R(0x8)                                  # heat: soft temp only
        f._check_rails(rows)
        assert [r["code"] for r in rows] == ["HOT"], rows
        assert "84.0C" in rows[0]["text"]

        rows.clear()
        f._run = _R(0x6)                                  # capped + throttled,
        f._check_rails(rows)                              # same hot SoC
        assert [r["code"] for r in rows] == ["HOT"], "one row per CAUSE — the "
        # response bits flickering must not read as the body changing its mind

        rows.clear()
        f._run = _R(0x5)                                  # sagging input, and
        f._check_rails(rows)                              # the SoC coping
        assert [r["code"] for r in rows] == ["UNDERVOLT"], rows
        assert "throttled to cope" in rows[0]["text"]

        rows.clear()
        f._run = _R(0x2, "62.0'C")                        # capped, temp fine
        f._check_rails(rows)
        assert [r["code"] for r in rows] == ["THROTTLED"], rows

        rows.clear()
        f._run = _R(0x0)                                  # healthy is silent
        f._check_rails(rows)
        assert rows == []
    finally:
        f._run = orig


def test_bus_labels_cover_every_expected_bus():
    """Every bus we police has a short name for it — an unnamed fault is one
    nobody reads, and `1.0 FREE` named nothing."""
    assert set(fault.BUS_LABEL) == set(fault.BUS_OWNERS)
    for label in fault.BUS_LABEL.values():
        assert len(f"{label} NODRV") <= 14      # the face draws 14 chars


def test_face_labels_carry_the_evidence_and_fit():
    """The PAIN page draws the row's `face`. A code with no number is a name
    with no use: 'HOT' tells you nothing you can act on, 'HOT 86C' does."""
    assert len("HOT 86C") <= 14
    for r in fault.sweep()["rows"]:
        label = r.get("face") or r["code"]
        assert len(label) <= 14, f"{label!r} will not fit the face"


def test_a_ghost_reading_is_not_a_reading(monkeypatch):
    """Found live: with the temp sensor unplugged, the cortex still held 23.8C
    from 100 minutes earlier and the sweep called the body well. 'Is there a
    value?' is not the same question as 'is it live?'."""
    import time as _t
    import loa.cortex as cortex
    import loa.fault as f

    now = _t.time()
    monkeypatch.setattr(f, "_feed_state", lambda *a, **k: {
        "temp_c": 23.8, "temp_ts": now - 6002,          # 100 minutes old
        "pressure_hpa": None, "baro_ts": None,
    })
    rows = []
    f._check_sensors(rows)
    assert [r["code"] for r in rows] == ["TEMP STALE"], rows
    assert "100 min ago" in rows[0]["text"]
    assert len(rows[0]["face"]) <= 14, "must fit the face"

    # fresh reading: silent
    monkeypatch.setattr(f, "_feed_state", lambda *a, **k: {
        "temp_c": 23.8, "temp_ts": now - 5,
        "pressure_hpa": None, "baro_ts": None,
    })
    rows.clear()
    f._check_sensors(rows)
    assert rows == []

    # absent sensor: not this check's business (the face shows absence)
    monkeypatch.setattr(f, "_feed_state", lambda *a, **k: {
        "temp_c": None, "temp_ts": None,
        "pressure_hpa": None, "baro_ts": None,
    })
    rows.clear()
    f._check_sensors(rows)
    assert rows == []


def test_quiet_format_prints_only_faults():
    rep = {"ts": 0.0, "boot": "test", "faults": 1, "warns": 1, "rows": [
        {"level": "fault", "code": "OLED-DEAD", "text": "nothing holds it"},
        {"level": "warn", "code": "NIGGLE", "text": "minor"},
    ]}
    out = fault.format_report(rep, quiet=True)
    assert "OLED-DEAD" in out
    assert "minor" not in out
