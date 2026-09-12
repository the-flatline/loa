#!/usr/bin/env python3
"""Standalone BMP180/BMP085 validation — stdlib only, I2C chardev.

Run on the Pi:
    ssh flatline@<loa> '/home/flatline/venv/bin/python3 -' < bmp180_check.py [bus] [addr]
"""
import fcntl
import os
import struct
import sys
import time

bus = int(sys.argv[1]) if len(sys.argv) > 1 else 1
addr = int(sys.argv[2], 0) if len(sys.argv) > 2 else 0x77
path = f"/dev/i2c-{bus}"
I2C_SLAVE = 0x0703


def rd(fd, reg, n):
    os.write(fd, bytes([reg]))
    return os.read(fd, n)


def wr(fd, reg, val):
    os.write(fd, bytes([reg, val]))


fd = os.open(path, os.O_RDWR)
fcntl.ioctl(fd, I2C_SLAVE, addr)

chip = rd(fd, 0xD0, 1)[0]
print(f"chip id: 0x{chip:02x} ({'BMP180/BMP085' if chip == 0x55 else 'UNEXPECTED'})")

cal = rd(fd, 0xAA, 22)
ac1, ac2, ac3, ac4, ac5, ac6, b1, b2, mb, mc, md = struct.unpack(">11h", cal)
ac4 &= 0xFFFF
ac5 &= 0xFFFF
ac6 &= 0xFFFF
print(f"cal: ac1={ac1} ac2={ac2} ac3={ac3} ac4={ac4} ac5={ac5} ac6={ac6} "
      f"b1={b1} b2={b2} mb={mb} mc={mc} md={md}")

# temperature
wr(fd, 0xF4, 0x2E)
time.sleep(0.005)
ut = struct.unpack(">H", rd(fd, 0xF6, 2))[0]

x1 = ((ut - ac6) * ac5) >> 15
x2 = (mc << 11) // (x1 + md)
b5 = x1 + x2
temp_c = (b5 + 8) >> 4
print(f"UT={ut}  temp: {temp_c / 10.0:.1f} C")

# pressure, oss=0
wr(fd, 0xF4, 0x34)
time.sleep(0.005)
raw = rd(fd, 0xF6, 3)
up = ((raw[0] << 16) | (raw[1] << 8) | raw[2]) >> 8

b6 = b5 - 4000
x1 = (b2 * ((b6 * b6) >> 12)) >> 11
x2 = (ac2 * b6) >> 11
x3 = x1 + x2
b3 = (((ac1 * 4 + x3) + 2) >> 2)
x1 = (ac3 * b6) >> 13
x2 = (b1 * ((b6 * b6) >> 12)) >> 16
x3 = ((x1 + x2) + 2) >> 2
b4 = (ac4 * (x3 + 32768)) >> 15
b7 = (up - b3) * 50000
if b7 < 0x80000000:
    p = (b7 * 2) // b4
else:
    p = (b7 // b4) * 2
x1 = (p >> 8) * (p >> 8)
x1 = (x1 * 3038) >> 16
x2 = (-7357 * p) >> 16
p = p + ((x1 + x2 + 3791) >> 4)
print(f"UP={up}  pressure: {p / 100.0:.1f} hPa")

os.close(fd)