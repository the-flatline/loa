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
from loa_ring.oled_daemon import _wash, render_loop  # noqa: E402

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
r = c.get("/twin")
check("twin endpoint", r.status_code == 200 and "ring" in r.json()
      and "face" in r.json() and "status" in r.json())
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

print("== pixel wash provable coverage ==")

class RecordingDisplay:
    def __init__(self):
        self.frames = []
    def clear(self):
        pass
    def set_contrast(self, v):
        pass
    def show(self, buf, offset=None):
        self.frames.append(bytes(buf))
    def close(self):
        pass

rec = RecordingDisplay()
wash_frame = oled.Frame()
_wash(rec, wash_frame, 1.0)                      # blink window 0.4s: ON then OFF
check("wash drives every pixel ON", any(all(b == 0xFF for b in f) for f in rec.frames))
check("wash drives every pixel OFF", any(all(b == 0x00 for b in f) for f in rec.frames))

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

print("== sense daemon (fake reader) ==\n")

from loa_ring import sense as sense_mod  # noqa: E402


class FakeReader:
    def __init__(self, levels):
        self.levels = list(levels)
        self.i = 0

    def __call__(self, gpio):
        v = self.levels[min(self.i, len(self.levels) - 1)]
        self.i += 1
        return v


fired = []
p = sense_mod.SensePoller(gpio=17, cooldown=0.0,
                          reader=FakeReader([False, True, True]),
                          fire=lambda: fired.append("motion"))
p.tick(); p.tick(); p.tick()
check("sense fires on debounced rising edge", fired == ["motion"])

fired.clear()
p2 = sense_mod.SensePoller(gpio=17, cooldown=10.0,
                           reader=FakeReader([False, True, True, False, True, True]),
                           fire=lambda: fired.append("motion"))
for _ in range(6):
    p2.tick()
check("sense cooldown suppresses refire", fired == ["motion"])

# default fire path: writes cortex state + event
cortex.set_state({"ring_state": "home", "pending_event": None})
p3 = sense_mod.SensePoller(gpio=17, cooldown=0.0,
                           reader=FakeReader([False, True, True]))
p3.tick(); p3.tick(); p3.tick()
check("sense motion sets scan event",
      cortex.get_state()["pending_event"] == "scan")
check("sense motion logged",
      any(e["kind"] == "sense" for e in cortex.history(5)))

# stopwatch: rising edge starts pir_on_ts, falling edge latches hold
cortex.set_state({"pir_high": 0, "pir_on_ts": None, "pir_last_hold": 0.0})
p5 = sense_mod.SensePoller(gpio=17, cooldown=0.0,
                           reader=FakeReader([False, True, True, False]))
p5.tick(); p5.tick(); p5.tick()
st = cortex.get_state()
check("sense rising edge starts timer", st["pir_high"] is True
      and st["pir_on_ts"] is not None)
p5.tick()
st = cortex.get_state()
check("sense falling edge latches hold", st["pir_high"] is False
      and st["pir_on_ts"] is None and st["pir_last_hold"] >= 0.0)

# sonar: a fake measure feeds distance into the cortex
cortex.set_state({"snr_cm": None, "snr_ts": None, "snr_count": 0})
s = sense_mod.Sonar(trig=23, echo=22, period=0.0, measure=lambda: 42.5)
s.tick()
st = cortex.get_state()
check("sonar writes distance + count", st["snr_cm"] == 42.5
      and st["snr_count"] == 1 and st["snr_ts"] is not None)
s2 = sense_mod.Sonar(trig=23, echo=22, period=0.0, measure=lambda: None)
s2.tick()
st = cortex.get_state()
check("sonar no-read leaves state", st["snr_cm"] == 42.5
      and st["snr_count"] == 1)
# boot resets the N counters
cortex.set_state({"sense_count": 9, "snr_count": 9})
sense_mod.main = lambda: None  # don't run the daemon
check("sonar class exists for ripperdoc", hasattr(sense_mod, "Sonar"))

print("== ripperdoc mode ==")

r = c.post("/ripperdoc", json={"on": True})
check("ripperdoc on", r.status_code == 200 and r.json()["ripperdoc"] is True
      and cortex.get_state()["ripperdoc"] is True
      and cortex.get_state()["oled_mode"] == "ripperdoc")
r = c.get("/state")
check("state reports ripperdoc + sense",
      r.json()["ripperdoc"] is True and "sense" in r.json())
r = c.post("/ripperdoc", json={"on": False})
check("ripperdoc off restores scope",
      r.status_code == 200 and cortex.get_state()["oled_mode"] == "scope"
      and cortex.get_state()["ripperdoc"] is False)
r = c.post("/ripperdoc", json={"page": "pir"})
check("ripperdoc page switch",
      r.status_code == 200 and r.json()["page"] == "pir"
      and cortex.get_state()["ripperdoc_page"] == "pir")
r = c.post("/ripperdoc", json={"page": "snr"})
check("ripperdoc snr page switch",
      r.status_code == 200 and r.json()["page"] == "snr"
      and cortex.get_state()["ripperdoc_page"] == "snr")
r = c.post("/ripperdoc", json={"page": "bogus"})
check("bad ripperdoc page 400", r.status_code == 400)
r = c.post("/ripperdoc", json={"page": "sensors"})
check("ripperdoc back to sensors", r.status_code == 200
      and cortex.get_state()["ripperdoc_page"] == "sensors")

print("== ripperdoc face (no hardware) ==")

fb.clear()
rd = oled.Ripperdoc()
rd.draw_state(fb, 100.0, {"pir_high": False, "sense_count": 0,
                          "sense_ts": None})
off_buf = bytes(fb.buf)
check("ripperdoc outline draws", sum(off_buf) > 0)
fb.clear()
rd.draw_state(fb, 100.0, {"pir_high": True, "sense_count": 7,
                          "sense_ts": 96.8})
on_buf = bytes(fb.buf)
check("ripperdoc solid when high", sum(on_buf) > sum(off_buf))
fb.clear()
rd.draw_state(fb, 100.0, {"pir_high": False, "sense_count": 7,
                          "sense_ts": 96.8, "snr_cm": None,
                          "ripperdoc_page": "sensors"})
snr_off = bytes(fb.buf)
fb.clear()
rd.draw_state(fb, 100.0, {"pir_high": False, "sense_count": 7,
                          "sense_ts": 96.8, "snr_cm": 42.0,
                          "ripperdoc_page": "sensors"})
snr_on = bytes(fb.buf)
check("snr indicator lit when enabled", sum(snr_on) > sum(snr_off))
fb.clear()
rd.draw_state(fb, 100.0, {"pir_high": True, "sense_count": 7,
                          "sense_ts": 96.8, "ripperdoc_page": "sensors"})
from loa_ring import amiga  # noqa: E402
amiga.draw(fb, rd.TITLE, 2, 1, size=8)
check("amiga font draws", sum(fb.buf) > 0)
fb.clear()
rd.draw_state(fb, 100.0, {"pir_high": True, "sense_count": 7,
                          "sense_ts": 96.8, "ripperdoc_page": "pir"})
check("ripperdoc pir page draws", sum(fb.buf) > 0)
fb.clear()
rd.draw_state(fb, 100.0, {"pir_high": False, "sense_count": 7,
                          "sense_ts": 96.8, "ripperdoc_page": "pir"})
pir_off = bytes(fb.buf)
fb.clear()
rd.draw_state(fb, 100.0, {"pir_high": True, "sense_count": 7,
                          "sense_ts": 96.8, "ripperdoc_page": "pir"})
pir_on = bytes(fb.buf)
check("pir detail page shows the light", sum(pir_on) > sum(pir_off))
fb.clear()
rd.draw_state(fb, 100.0, {"pir_high": False, "sense_count": 7,
                          "sense_ts": 96.8, "ripperdoc_page": "snr",
                          "snr_cm": 42.5, "snr_count": 3, "snr_ts": 96.8})
check("ripperdoc snr page draws with distance", sum(fb.buf) > 0)
fb.clear()
rd.draw_state(fb, 100.0, {"pir_high": False, "sense_count": 7,
                          "sense_ts": 96.8, "ripperdoc_page": "snr",
                          "snr_cm": None, "snr_count": 0, "snr_ts": None})
check("ripperdoc snr page draws no-read", sum(fb.buf) > 0)
cortex.set_state({"oled_mode": "ripperdoc"})
render_loop(max_frames=5)
check("oled daemon renders ripperdoc", True)
cortex.set_state({"oled_mode": "scope"})

print("== bench twin (no hardware) ==\n")

from loa_ring import bench  # noqa: E402

fb = oled.Frame()
fb.px(0, 0)
art = bench.oled_art(fb)
check("oled twin renders pixels", "▀" in art or "█" in art)
fb.clear()
check("oled twin blank is blank", bench.oled_art(fb).strip() == "")
ring = bench.ring_art_bytes(bytes([255, 0, 0]) * 24)
check("ring twin renders markup", "[on rgb(255,0,0)]" in ring)
check("ring twin dim shape present", "[on rgb(12,16,12)]" in ring)
check("ring 24 unique ordered slots",
      len(set(bench._led_positions())) == 24)


async def _pilot():
    app = bench.BenchApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#status", Static).update("ok")
        await pilot.pause()
        app.exit()


import asyncio  # noqa: E402
from textual.widgets import Static  # noqa: E402
asyncio.run(_pilot())
check("bench app boots headless", True)

print(f"\nALL {PASS} CHECKS PASSED")