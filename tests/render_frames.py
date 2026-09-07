#!/usr/bin/env python3
"""Render the face animations to PNGs (no hardware). For the scrapbook."""
import os
import struct
import sys
import time
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loa_ring import oled

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frames")
os.makedirs(OUT, exist_ok=True)


def write_png(path, w, h, rows):
    raw = bytearray()
    for row in rows:
        raw.append(0)          # filter type 0
        raw.extend(row)

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        c += struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        return c

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
           + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)


def frame_to_rows(fb):
    rows = []
    for y in range(oled.HEIGHT):
        page = y >> 3
        bit = 1 << (y & 7)
        rows.append([
            255 if fb.buf[page * oled.WIDTH + x] & bit else 0
            for x in range(oled.WIDTH)
        ])
    return rows


def save(name, fb, t):
    fb.draw(fb, t) if False else None
    write_png(os.path.join(OUT, f"{name}.png"), oled.WIDTH, oled.HEIGHT,
              frame_to_rows(fb))
    print(f"wrote {name}.png")


fb = oled.Frame()

# scope with a live blip mid-decay + scan bar somewhere else
s = oled.Scope()
s.next_blip = time.time() - 1.0
s.tick(1.0)                      # spawn the blip
s.blips = [(s.blips[0][0], 0.7)]
fb.clear()
s.draw(fb, 1.5)                  # scan bar at ~x60
write_png(os.path.join(OUT, "face-scope.png"), oled.WIDTH, oled.HEIGHT,
          frame_to_rows(fb))
print("wrote face-scope.png")

fb.clear()
oled.ECG().draw(fb, 0.1)
write_png(os.path.join(OUT, "face-ecg.png"), oled.WIDTH, oled.HEIGHT,
          frame_to_rows(fb))
print("wrote face-ecg.png")

fb.clear()
oled.Ripple().draw(fb, 0.3)
write_png(os.path.join(OUT, "face-ripple.png"), oled.WIDTH, oled.HEIGHT,
          frame_to_rows(fb))
print("wrote face-ripple.png")

fb.clear()
oled.Noise().draw(fb, 2.5)
write_png(os.path.join(OUT, "face-noise.png"), oled.WIDTH, oled.HEIGHT,
          frame_to_rows(fb))
print("wrote face-noise.png")

fb.clear()
oled.Marquee("THE OLD GIRL").draw(fb, 3.0)
write_png(os.path.join(OUT, "face-marquee.png"), oled.WIDTH, oled.HEIGHT,
          frame_to_rows(fb))
print("wrote face-marquee.png")