"""The console reads the BODY, not the machine it happens to run on.

Found live 2026-09-12: running `ripperdoc` from dixie, every hotkey appeared
dead. The keys were fine — the POSTs reached the body and changed its page.
The console's *reads* went to cortex.get_state(), which opens whichever
cortex.db is on the local machine. On dixie that is dixie's own empty database
(calm / home / page sensors), so the screen never moved while the body was
alarmed and hurting. RING_TOPIC had the same defect: a body path read locally,
so the ring pane was permanently "RING OFFLINE" off-body.
"""
import base64

import loa.ripperdoc as rd


def test_console_never_reads_a_local_cortex_db(monkeypatch):
    def explode(*a, **kw):                      # the old path, now forbidden
        raise AssertionError("console read a LOCAL cortex db — it must ask "
                             "the body over HTTP instead")

    monkeypatch.setattr(rd.cortex, "get_state", explode)
    monkeypatch.setattr(rd, "_get", lambda path: {
        "mood": {"feeling": "alarmed"},
        "ring": {"state": "alarm"},
        "oled": {"mode": "ripperdoc", "text": "!! FLATLINE !!"},
        "ripperdoc": True,
        "ripperdoc_page": "snr",
        "condition": "hurts",
        "sense": {"pir_high": True, "count": 7, "last_hold": 4.2},
        "power": {"3V3_SYS_V": 3.331},
    })

    st = rd.body_state()
    assert st["mood"] == "alarmed" and st["ring_state"] == "alarm"
    assert st["ripperdoc_page"] == "snr", "must report the BODY's page"
    assert st["power"]["3V3_SYS_V"] == 3.331, "power comes from the body too"

    line = rd.fetch_sense()
    assert "alarmed" in line and "snr" in line and "SOLID" in line


def test_ring_twin_comes_over_the_wire(monkeypatch):
    """The ring bytes must be fetched. Reading /dev/shm/loa-ring.bin is a
    body-local path: off-body it is always empty, hence a permanent OFFLINE."""
    asked = []
    frame = bytes(range(72))

    def fake_get(path):
        asked.append(path)
        return {"ring": base64.b64encode(frame).decode(), "face": "x"}

    monkeypatch.setattr(rd, "_get", fake_get)
    raw = base64.b64decode(fake_get("/twin")["ring"])
    assert asked == ["/twin"]
    assert len(raw) == 72, "the ring twin must round-trip"


def test_an_unreachable_body_says_so(monkeypatch):
    def dead(path):
        raise OSError("connection refused")

    monkeypatch.setattr(rd, "_get", dead)
    assert "unreachable" in rd.fetch_sense()
