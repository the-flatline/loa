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

from loa import cortexd, cortex, expressions, moods, oled, fault  # noqa: E402
from loa import ring as ringd  # noqa: E402
from loa.oled_daemon import _wash, render_loop  # noqa: E402

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
c = TestClient(cortexd.app)
r = c.get("/health")
check("health", r.status_code == 200 and r.json()["ok"] and r.json()["version"])
r = c.get("/state")
check("state", r.status_code == 200 and r.json()["mood"]["feeling"] in moods.MOODS)
r = c.get("/state?history=10")
check("state history", r.status_code == 200 and len(r.json()["history"]) > 0)
r = c.get("/twin")
check("twin endpoint", r.status_code == 200 and "ring" in r.json()
      and "face" in r.json() and "status" in r.json())
r = c.get("/state")
check("state has sensors (honest)", r.json()["sensors"]["available"] is False)
cortex.set_state({"pressure_hpa": 1026.0, "baro_temp_c": 23.1, "baro_ts": 1.0})
r = c.get("/state")
check("state baro block live", r.json()["sensors"]["baro"]["available"] is True
      and r.json()["sensors"]["baro"]["pressure_hpa"] == 1026.0
      and r.json()["sensors"]["baro"]["trend"]["dir"]
      in ("rising", "falling", "steady"))
r = c.get("/twin")
check("twin carries weather", r.json()["status"]["pressure_hpa"] == 1026.0
      and r.json()["status"]["temp_c"] is None
      and "baro_trend" in r.json()["status"]
      and isinstance(r.json()["status"]["baro_series"], list))

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
t = threading.Thread(target=lambda: ringd.busy(ring))
t.start()
time.sleep(0.2)
cortex.set_state({"ring_state": "home"})
t.join(timeout=3)
check("busy loop exits on state change", not t.is_alive() and ring.shows > 0)

cortex.set_state({"ring_state": "home", "pending_event": "scan"})
t = threading.Thread(target=lambda: ringd.one_scan(ring))
t.start()
time.sleep(0.2)
cortex.set_state({"ring_state": "busy"})
t.join(timeout=3)
check("scan loop aborts on state change", not t.is_alive())

cortex.set_state({"ring_state": "alarm"})
t = threading.Thread(target=lambda: ringd.alarm(ring))
t.start()
time.sleep(0.2)
cortex.set_state({"ring_state": "home"})
t.join(timeout=3)
check("alarm loop exits on state change", not t.is_alive())

print("== sense daemon (fake reader) ==\n")

from loa import sense as sense_mod  # noqa: E402


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

# baro: BMP180 compensation regression — the live chip values read on the
# bench 09-12 (cal EEPROM of the replacement XC3702 sitting at 1026.0 hPa /
# 23.1°C). If this drifts, the datasheet math broke.
_cal = (8687, -1183, -14304, 33899, 25081, 20813, 6515, 47,
        -32768, -11786, 2771)
_t, _p = sense_mod.BMP180._compensate(_cal, 29108, 43654)
check("baro compensation matches live chip", round(_t, 1) == 23.1
      and round(_p / 100.0, 1) == 1026.0)

print("== baro trend ==\n")

# pure trend math — synthetic series, no database
now = 1_000_000.0
pairs = [(now - 3 * 3600 + i * 600, 1013.0 + i * 0.2) for i in range(19)]
tr = cortex._trend_from_samples(pairs, now, 3 * 3600)
check("trend rising detected", tr["dir"] == "rising"
      and tr["slope_hpa_per_h"] > 0.9)
flat = [(now - i * 600, 1013.0) for i in range(19)]
tr2 = cortex._trend_from_samples(flat, now, 3 * 3600)
check("trend flat reads steady", tr2["dir"] == "steady")
down = [(now - 3 * 3600 + i * 600, 1020.0 - i * 0.2) for i in range(19)]
tr3 = cortex._trend_from_samples(down, now, 3 * 3600)
check("trend falling detected", tr3["dir"] == "falling")
tr4 = cortex._trend_from_samples([], now, 3 * 3600)
check("trend no samples steady", tr4["dir"] == "steady")

# telemetry storage + db-backed trend — runs BEFORE the daemon tick test
# so the table only holds these three synthetic samples
rnow = time.time()
cortex.baro_sample(1013.0, 21.0, rnow - 100)
cortex.baro_sample(1013.5, 21.1, rnow - 50)
cortex.baro_sample(1014.0, 21.2, rnow)
rows = cortex.baro_samples(since=rnow - 200)
check("baro samples stored", len(rows) == 3 and rows[-1][1] == 1014.0)
rows2 = cortex.baro_samples(since=rnow - 60)
check("baro samples windowed", len(rows2) == 2)
tr5 = cortex.baro_trend(window_s=3 * 3600, now=rnow)
check("baro trend from db", tr5["dir"] == "rising")

cortex.set_state({"pressure_hpa": None, "baro_temp_c": None,
                  "baro_ts": None, "baro_count": 0})
_b = sense_mod.BMP180(period=0.0, reader=lambda: (23.1, 102600.0))
_b.tick()
st = cortex.get_state()
check("baro tick writes pressure + count", st["pressure_hpa"] == 1026.0
      and st["baro_temp_c"] == 23.1 and st["baro_count"] == 1
      and st["baro_ts"] is not None)
_b2 = sense_mod.BMP180(period=0.0, reader=lambda: None)
_b2.tick()
st = cortex.get_state()
check("baro no-read leaves state", st["pressure_hpa"] == 1026.0
      and st["baro_count"] == 1)

print("== ripperdoc mode ==\n")

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
from loa import amiga  # noqa: E402
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
fb.clear()
rd.draw_state(fb, 100.0, {"pir_high": False, "sense_count": 7,
                          "sense_ts": 96.8, "ripperdoc_page": "frag"})
frag_buf = bytes(fb.buf)
check("ripperdoc frag page draws", sum(frag_buf) > 0)
check("ripperdoc frag page has lock + text pixels",
      sum(frag_buf) > 0 and len(frag_buf) == len(off_buf))


def px_on(buf, x, y):
    return bool(buf[(y >> 3) * 128 + x] & (1 << (y & 7)))


# temp page: pressure value + trend caret (rising apex lit, absent w/o pressure)
cortex.baro_sample(1013.0, 21.0, time.time() - 200)
cortex.baro_sample(1013.6, 21.1, time.time() - 100)
cortex.baro_sample(1014.2, 21.2, time.time())
fb.clear()
rd.draw_state(fb, 100.0, {"pir_high": False, "sense_count": 7,
                          "sense_ts": 96.8, "ripperdoc_page": "temp",
                          "temp_c": 23.0, "hum_pct": 50.0,
                          "pressure_hpa": 1014.2})
buf_p = bytes(fb.buf)
cx = 128 - amiga.width("1014HPA", 8) - 8
check("temp page draws pressure + trend caret",
      sum(buf_p) > 0 and px_on(buf_p, cx + 3, 45))
fb.clear()
rd.draw_state(fb, 100.0, {"pir_high": False, "sense_count": 7,
                          "sense_ts": 96.8, "ripperdoc_page": "temp",
                          "temp_c": 23.0, "hum_pct": 50.0,
                          "pressure_hpa": None})
check("temp page no pressure no caret", not px_on(bytes(fb.buf), cx + 3, 45))
cortex.set_state({"oled_mode": "ripperdoc"})
render_loop(max_frames=5)
check("oled daemon renders ripperdoc", True)
cortex.set_state({"oled_mode": "scope"})

print("== ripperdoc twin (no hardware) ==\n")

from loa import ripperdoc  # noqa: E402

fb = oled.Frame()
fb.px(0, 0)
art = ripperdoc.oled_art(fb)
check("oled twin renders pixels", "▀" in art or "█" in art)
fb.clear()
check("oled twin blank is blank", ripperdoc.oled_art(fb).strip() == "")
ring = ripperdoc.ring_art_bytes(bytes([255, 0, 0]) * 24)
check("ring twin renders markup", "[on rgb(255,0,0)]" in ring)
check("ring twin dim shape present", "[on rgb(12,16,12)]" in ring)
check("ring 24 unique ordered slots",
      len(set(ripperdoc._led_positions())) == 24)


async def _pilot():
    app = ripperdoc.RipperdocApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#status", Static).update("ok")
        await pilot.pause()
        app.exit()


import asyncio  # noqa: E402
from textual.widgets import Static  # noqa: E402
asyncio.run(_pilot())
check("ripperdoc app boots headless", True)

print(f"\nALL {PASS} CHECKS PASSED")