#!/usr/bin/env python3
"""Cortex + API + daemon smoke tests. No hardware — spidev is absent here,
so the face degrades to NullDisplay and the ring gets a FakeRing.

Run from the repo root with the venv python:
    .venv/bin/python tests/test_cortex.py
"""
import os
import sys
import tempfile
import threading
import time

# Isolate the cortex DB before anything connects.
os.environ["LOA_CORTEX_DB"] = os.path.join(tempfile.mkdtemp(), "cortex-test.db")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from loa_ring import api, cortex, expressions, moods, oled, presence  # noqa: E402
from loa_ring.oled_daemon import render_loop  # noqa: E402

PASS = 0


def check(name, cond):
    global PASS
    assert cond, f"FAIL: {name}"
    PASS += 1
    print(f"  ok  {name}")


print("== cortex ==")
cortex.set_state({"mood": "busy", "ring_state": "busy"})
st = cortex.get_state()
check("state write/read", st["mood"] == "busy" and st["ring_state"] == "busy")
cortex.set_state({"pending_event": "scan"})
check("pending event set", cortex.get_state()["pending_event"] == "scan")
cortex.clear_event()
check("pending event cleared", cortex.get_state()["pending_event"] is None)
cortex.log_event("test", {"n": 1})
check("history has event", any(e["kind"] == "test" for e in cortex.history(5)))

print("== api ==")
c = TestClient(api.app)
r = c.get("/health")
check("health", r.status_code == 200 and r.json()["ok"] and r.json()["version"])
r = c.get("/state")
check("state", r.status_code == 200 and r.json()["mood"]["feeling"] in moods.MOODS)
r = c.get("/state?history=10")
check("state history", r.status_code == 200 and len(r.json()["history"]) > 0)
check("state has sensors (honest)", r.json()["sensors"]["available"] is False)

for mood in moods.MOODS:
    r = c.post("/feel", json={"feeling": mood})
    check(f"feel {mood}", r.status_code == 200 and r.json()["ok"])
    st = cortex.get_state()
    m = moods.MOODS[mood]
    if m["ring"] in ("home", "busy", "alarm"):
        check(f"  ring sustained {mood}", st["ring_state"] == m["ring"]
              and st["pending_event"] is None)
    else:
        check(f"  ring event {mood}", st["pending_event"] == m["ring"])
    check(f"  oled {mood}", st["oled_mode"] == m["oled"]["mode"])

r = c.post("/feel", json={"feeling": "nope"})
check("bad feel 400", r.status_code == 400)

r = c.post("/express", json={"expression": "happy"})
check("express named", r.status_code == 200
      and cortex.get_state()["oled_mode"] == "text"
      and cortex.get_state()["expression"] == "happy")
r = c.post("/express", json={"expression": "custom", "text": "HELLO"})
check("express custom", r.status_code == 200
      and cortex.get_state()["oled_text"] == "HELLO")
r = c.post("/express", json={"expression": "custom"})
check("custom without text 400", r.status_code == 400)
r = c.post("/express", json={"expression": "nope"})
check("bad express 400", r.status_code == 400)

r = c.post("/ring", json={"state": "scan"})
check("ring scan event", r.status_code == 200
      and cortex.get_state()["pending_event"] == "scan")
cortex.clear_event()
r = c.post("/ring", json={"state": "busy"})
check("ring busy sustained", r.status_code == 200
      and cortex.get_state()["ring_state"] == "busy")
cortex.set_state({"ring_state": "home"})
r = c.post("/ring", json={"state": "bogus"})
check("bad ring 400", r.status_code == 400)

r = c.post("/display", json={"mode": "ecg"})
check("display ecg", r.status_code == 200
      and cortex.get_state()["oled_mode"] == "ecg")
r = c.post("/display", json={"mode": "showoff"})
check("display showoff", r.status_code == 200
      and cortex.get_state()["oled_mode"] == "showoff")
r = c.post("/display", json={"mode": "text", "text": "TEST", "dim": True})
check("display text dim", r.status_code == 200
      and cortex.get_state()["oled_text"] == "TEST"
      and cortex.get_state()["oled_dim"] is True)
r = c.post("/display", json={"mode": "bogus"})
check("bad display 400", r.status_code == 400)

print("== oled animations (no hardware) ==")
fb = oled.Frame()
for name, maker in [("scope", oled.Scope), ("ecg", oled.ECG),
                    ("ripple", oled.Ripple), ("noise", oled.Noise),
                    ("text", lambda: oled.Marquee("THE OLD GIRL")),
                    ("showoff", oled.Showoff)]:
    fb.clear()
    maker().draw(fb, time.time())
    check(f"oled {name} draws pixels", sum(fb.buf) > 0)

print("== oled daemon smoke ==")
render_loop(max_frames=10)
check("oled daemon runs (NullDisplay)", True)

print("== presence daemon loops (FakeRing) ==")


class FakeRing:
    def __init__(self):
        self.shows = 0

    def show(self, frame):
        self.shows += 1

    def close(self):
        pass


ring = FakeRing()

cortex.set_state({"ring_state": "busy"})
t = threading.Thread(target=lambda: presence.busy(ring))
t.start()
time.sleep(0.2)
cortex.set_state({"ring_state": "home"})
t.join(timeout=3)
check("busy loop exits on state change", not t.is_alive() and ring.shows > 0)

cortex.set_state({"ring_state": "home", "pending_event": "scan"})
t = threading.Thread(target=lambda: presence.one_scan(ring))
t.start()
time.sleep(0.2)
cortex.set_state({"ring_state": "busy"})
t.join(timeout=3)
check("scan loop aborts on state change", not t.is_alive())

cortex.set_state({"ring_state": "alarm"})
t = threading.Thread(target=lambda: presence.alarm(ring))
t.start()
time.sleep(0.2)
cortex.set_state({"ring_state": "home"})
t.join(timeout=3)
check("alarm loop exits on state change", not t.is_alive())

print(f"\nALL {PASS} CHECKS PASSED")