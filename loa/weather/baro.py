"""baro — the barometric pressure driver (XC3702, BMP180-class) on I2C 0x77.

The weather daemon's third reading, in the weather daemon's folder. The XC3702
is BMP180-class (chip ID 0x55), NOT a BMP280 — registers (0xAA-0xBF cal, 0xF4
ctrl, 0xF6 out) and the compensation differ. Stdlib only via the i2c chardev
(/dev/i2c-N + I2C_SLAVE) — no smbus. Calibration is read once at first use. A
missing bus or a dead chip returns None instead of hanging the daemon.

Reads are PUBLISHED on the `baro` topic as pressure_hpa / baro_temp_c /
baro_ts / baro_count. Nothing here reads the cortex's state and nothing here
writes it: the driver holds its own count, and the body's memory is the
cortex's business.
"""

import fcntl
import os
import struct
import sys
import threading
import time

from ..sense import publish

DEFAULT_BARO_BUS = 1
DEFAULT_BARO_ADDR = 0x77    # XC3702 sits at 0x77 (SDO high) — verified 09-12
DEFAULT_BARO_PERIOD = 10.0
BARO_MEAS_WAIT = 0.005      # oss=0: 4.5ms per datasheet; 5ms covers it


class BMP180:
    """Barometric pressure + onboard temp on I2C (the REMOTE weather board).

    The XC3702 is BMP180-class (chip ID 0x55), NOT a BMP280 — registers
    (0xAA-0xBF cal, 0xF4 ctrl, 0xF6 out) and the compensation differ.
    Stdlib only via the i2c chardev (/dev/i2c-N + I2C_SLAVE) — no smbus.
    Calibration is read once at first use. A missing bus or a dead chip
    returns None instead of hanging the daemon.

    Reads land in cortex as pressure_hpa / baro_temp_c / baro_ts /
    baro_count so the ripperdoc board and the live can show the air.
    """

    def __init__(self, bus=DEFAULT_BARO_BUS, addr=DEFAULT_BARO_ADDR,
                 period=DEFAULT_BARO_PERIOD, reader=None):
        self.path = f"/dev/i2c-{bus}"
        self.addr = addr
        self.period = period
        self.reader = reader or self._hw_read
        self._stop = threading.Event()
        self._fails = 0
        self._count = 0
        self._cal = None

    # -- hardware -------------------------------------------------------

    def _open(self):
        fd = os.open(self.path, os.O_RDWR)
        fcntl.ioctl(fd, 0x0703, self.addr)  # I2C_SLAVE
        return fd

    @staticmethod
    def _read_reg(fd, reg, n):
        os.write(fd, bytes([reg]))
        return os.read(fd, n)

    def _load_cal(self, fd):
        if self._cal is not None:
            return self._cal
        data = self._read_reg(fd, 0xAA, 22)
        ac1, ac2, ac3, ac4, ac5, ac6, b1, b2, mb, mc, md = \
            struct.unpack(">11h", data)
        self._cal = (ac1, ac2, ac3, ac4 & 0xFFFF, ac5 & 0xFFFF,
                     ac6 & 0xFFFF, b1, b2, mb, mc, md)
        return self._cal

    def _hw_read(self):
        """Read temp + pressure; returns (temp_c, pressure_pa) or raises."""
        fd = self._open()
        try:
            cal = self._load_cal(fd)
            os.write(fd, bytes([0xF4, 0x2E]))        # temp, oss=none
            time.sleep(BARO_MEAS_WAIT)
            ut = struct.unpack(">H", self._read_reg(fd, 0xF6, 2))[0]
            os.write(fd, bytes([0xF4, 0x34]))        # pressure, oss=0
            time.sleep(BARO_MEAS_WAIT)
            raw = self._read_reg(fd, 0xF6, 3)
            up = ((raw[0] << 16) | (raw[1] << 8) | raw[2]) >> 8
            return self._compensate(cal, ut, up)
        finally:
            os.close(fd)

    @staticmethod
    def _compensate(cal, ut, up):
        """BMP180/BMP085 datasheet compensation (oss=0) -> (temp_c, Pa)."""
        ac1, ac2, ac3, ac4, ac5, ac6, b1, b2, mb, mc, md = cal
        x1 = ((ut - ac6) * ac5) >> 15
        x2 = (mc << 11) // (x1 + md)
        b5 = x1 + x2
        temp_c = ((b5 + 8) >> 4) / 10.0
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
        return temp_c, p

    # -- daemon ---------------------------------------------------------

    def tick(self):
        try:
            v = self.reader()
        except OSError:
            v = None
        if v is None:
            self._fails += 1
            if self._fails == 1 or self._fails % 10 == 0:
                print(f"baro: read failed ({self._fails}x) {self.path} "
                      f"0x{self.addr:02x}", file=sys.stderr, flush=True)
            return
        self._fails = 0
        temp_c, pa = v
        now = time.time()
        # The driver holds its own count. Reaching into the cortex's state for a
        # counter is the same cross-service read as reaching for the reading —
        # and the count is per-boot anyway (see motion.py): a rebooted body
        # starts at zero, so there is nothing here to ask the body for.
        self._count += 1
        # The sample is recorded by the CORTEX, not here: the cortex is the only
        # writer of the store, and a driver reaching into it is the exact
        # cross-service write this design removes. The reading goes up; the
        # body's memory is the cortex's business.
        publish({"pressure_hpa": pa / 100.0, "baro_temp_c": temp_c,
                 "baro_ts": now, "baro_count": self._count}, topic="baro")

    def run(self):
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self.period)

    def stop(self):
        self._stop.set()
