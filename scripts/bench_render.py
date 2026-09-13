#!/usr/bin/env python3
"""How long does ONE frame cost, by itself?

The question the 120fps plan turns on: which wall do we hit first — the
renderer, the wire, or the glass? This measures the pure renderers with no
socket, no panel and no Textual in the way, so the number it prints is a FLOOR
for any architecture we pick. Whatever the loop adds is on top.

    uv run python scripts/bench_render.py [seconds]

Prints fps and ms/frame for the face (1024 B) and the ring (72 B), per mode.
Written to be run ON THE BODY (the machine that would do the work) — a timing
taken off-body is about the wrong CPU.
"""
import sys
import time

from loa.cortex import frames, ring

#: State shaped like the cortex hands it over, so no renderer takes a shortcut
#: down a branch a live body would not take.
STATE = {
    "mood": "calm", "ring_state": "home", "oled_mode": "scope",
    "page": "sensors", "condition": "fine",
    "power": {"3V3_SYS_V": 3.31, "EXT5V_V": 5.20, "throttled": 0},
    "pir_high": False, "pir_count": 12, "pir_last_hold": 0.4,
    "snr_cm": 41.0, "temp_c": 21.5, "hum_pct": 44.0,
    "faults": {"ts": time.time(), "rows": [], "faults": 0, "warns": 0},
    "frag": {"sealed": True, "entries": 3, "access_count": 6},
    "ring": bytes(72),
}


def bench(label, fn, secs):
    n = 0
    t0 = time.perf_counter()
    while True:
        fn()
        n += 1
        el = time.perf_counter() - t0
        if el >= secs:
            break
    ms = el / n * 1000.0
    print(f"  {label:<28s} {n / el:8.1f} fps   {ms:6.3f} ms/frame")
    return n / el


def main():
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 2.0
    print(f"render floor, {secs:.1f}s per mode — no socket, no panel, no TUI")

    face_r = frames.FaceRenderer()
    ring_r = ring.RingRenderer()
    worst = None
    for mode in ("scope", "ecg", "ripple", "noise", "showoff", "ripperdoc"):
        st = dict(STATE, oled_mode=mode)
        fps = bench("face " + mode, lambda st=st: face_r.render(st), secs)
        worst = fps if worst is None else min(worst, fps)
    st = dict(STATE, oled_mode="text", oled_text="BENCH")
    worst = min(worst, bench("face text", lambda: face_r.render(st), secs))

    print()
    bench("ring", lambda: ring_r.render(STATE), secs)

    print()
    print(f"worst mode        {worst:8.1f} fps   "
          f"{1000.0 / worst:6.3f} ms/frame  <-- the renderer's ceiling")
    print(f"120fps needs      {1000.0 / 120:8.3f} ms/frame of headroom")


if __name__ == "__main__":
    main()
