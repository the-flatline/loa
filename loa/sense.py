"""sense — the input daemon (loa-sense). Owns the senses: PIR on GPIO17
first, then the sonar and the weather board as they land.

The ring breathes as the body's presence display (loa-presence). This is the
other direction — the body noticing the room. On motion it fires the one-shot
scan event (a comet lap) and logs a 'sense' event to the cortex, so the brain
can see what the body felt.

Pin reading is a pinctrl subprocess (works as flatline on the Pi, no sudo).
Off-Pi, tests inject a fake reader.
"""

import fcntl
import os
import re
import struct
import subprocess
import sys
import threading
import time

from . import config
from . import cortex
from . import moods

POLL_PERIOD = 0.2
DEFAULT_GPIO = 17
DEFAULT_COOLDOWN = 5.0
DEFAULT_TRIG = 23
DEFAULT_ECHO = 22
DEFAULT_SNR_PERIOD = 1.0
SNR_TIMEOUT = 0.03          # 30ms echo wait — ~500cm ceiling
SNR_CHIPS = ("/dev/gpiochip0", "/dev/gpiochip4")
DEFAULT_TEMP_GPIO = 4       # REMOTE weather board DATA (XC4520, DHT11-class)
DEFAULT_TEMP_PERIOD = 10.0
DHT_PULSE_WINDOW = 0.02     # 20ms to collect pulses; stuck line must not hang
DHT_MAX_PULSES = 60
DHT_ONE_US = 60000          # high pulse longer than 60us = bit 1 — this
                            # clone's '0' drifts to ~47us, '1' starts at 70us
DEFAULT_BARO_BUS = 1
DEFAULT_BARO_ADDR = 0x77    # XC3702 sits at 0x77 (SDO high) — verified 09-12
DEFAULT_BARO_PERIOD = 10.0
BARO_MEAS_WAIT = 0.005      # oss=0: 4.5ms per datasheet; 5ms covers it

_HI = re.compile(r"\bhi\b")


def pinctrl_reader(gpio):
    """Read a GPIO level via pinctrl. True for hi, False for lo, None when
    the pin isn't reporting a level (unclaimed — caller re-asserts input)."""
    try:
        out = subprocess.run(["pinctrl", "get", str(gpio)],
                             capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    if "hi" not in out and "lo" not in out:
        return None
    return bool(_HI.search(out))


def set_input(gpio):
    """Claim the pin as an input so pinctrl reports its level."""
    try:
        subprocess.run(["pinctrl", "set", str(gpio), "ip"],
                       capture_output=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        pass


class SensePoller:
    """Poll a pin and fire on a debounced rising edge, with a cooldown so a
    person standing in front of the sensor doesn't machine-gun events."""

    def __init__(self, gpio=DEFAULT_GPIO, cooldown=DEFAULT_COOLDOWN,
                 reader=None, fire=None, assert_input=None,
                 poll_period=POLL_PERIOD):
        self.gpio = gpio
        self.cooldown = cooldown
        self.reader = reader or pinctrl_reader
        self.fire = fire or self._default_fire
        self.assert_input = assert_input or set_input
        self.poll_period = poll_period
        self._last = False
        self._pending = 0
        self._last_fire = 0.0

    def _default_fire(self):
        now = time.time()
        st = cortex.get_state()
        n = (st.get("sense_count") or 0) + 1
        cortex.set_state({"sense_ts": now, "sense_count": n})
        cortex.log_event("sense", {"kind": "pir", "gpio": self.gpio,
                                   "action": "motion", "count": n,
                                   "cooldown": self.cooldown})
        moods.apply_ring(cortex, "scan")

    def tick(self):
        level = self.reader(self.gpio)
        if level is None:
            self.assert_input(self.gpio)
            self._pending = 0
            return
        now = time.time()
        if level != self._last:
            if level:
                cortex.set_state({"pir_high": 1, "pir_on_ts": now})
            else:
                st = cortex.get_state()
                on_ts = st.get("pir_on_ts")
                hold = 0.0 if on_ts is None else now - on_ts
                cortex.set_state({"pir_high": 0, "pir_last_hold": hold,
                                  "pir_on_ts": None})
        if level:
            self._pending = 1 if not self._last else self._pending + 1
            if (self._pending >= 2
                    and now - self._last_fire >= self.cooldown):
                self._last_fire = now
                self.fire()
        else:
            self._pending = 0
        self._last = level

    def run(self):
        while True:
            self.tick()
            time.sleep(self.poll_period)


class Sonar:
    """Ultrasonic distance — TRIG pulse on one GPIO, time the ECHO high pulse
    on another. Needs microsecond timing, so gpiod (not pinctrl) drives it;
    off-Pi tests inject a fake measure().

    Reads land in cortex as snr_cm / snr_ts / snr_count so the ripperdoc
    board can show the LAST distance — the echo pulse is too short to see
    as a light, the number is the signal.
    """

    def __init__(self, trig=DEFAULT_TRIG, echo=DEFAULT_ECHO,
                 period=DEFAULT_SNR_PERIOD, measure=None, chip=None):
        self.trig = trig
        self.echo = echo
        self.period = period
        self.measure = measure or self._gpiod_measure
        self.chip = chip
        self._stop = threading.Event()

    def _gpiod_measure(self):
        try:
            import gpiod
            from gpiod.line import Direction, Value
        except ImportError:
            return None
        for path in ([self.chip] if self.chip else list(SNR_CHIPS)):
            try:
                req = gpiod.request_lines(
                    path, consumer="loa-snr",
                    config={
                        self.trig: gpiod.LineSettings(
                            direction=Direction.OUTPUT,
                            output_value=Value.INACTIVE),
                        self.echo: gpiod.LineSettings(
                            direction=Direction.INPUT),
                    })
            except OSError:
                continue
            try:
                t0 = time.perf_counter_ns()
                req.set_value(self.trig, Value.ACTIVE)
                while time.perf_counter_ns() - t0 < 10_000:
                    pass
                req.set_value(self.trig, Value.INACTIVE)
                t_start = time.perf_counter()
                while req.get_value(self.echo) is Value.INACTIVE:
                    if time.perf_counter() - t_start > SNR_TIMEOUT:
                        return None
                t_hi = time.perf_counter()
                while req.get_value(self.echo) is Value.ACTIVE:
                    if time.perf_counter() - t_hi > SNR_TIMEOUT:
                        return None
                t_end = time.perf_counter()
                us = (t_end - t_hi) * 1e6
                return us / 58.0
            finally:
                req.release()
        return None

    def tick(self):
        cm = self.measure()
        if cm is None:
            return
        now = time.time()
        n = (cortex.get_state().get("snr_count") or 0) + 1
        cortex.set_state({"snr_cm": cm, "snr_ts": now, "snr_count": n})

    def run(self):
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self.period)

    def stop(self):
        self._stop.set()


class DHT11:
    """DHT11-class temp/humidity on one GPIO (the REMOTE weather board).

    Start signal (20ms low), then the sensor pulls the line for a response
    pair and 40 data bits. The gpiod re-request between the start and the
    sampling window can miss the response pair, so pulses are collected and
    every alignment is tried until the frame checksum validates — a valid
    frame proves itself. Every wait is deadline-guarded: a stuck or
    disconnected line returns None instead of hanging the daemon.

    Reads land in cortex as temp_c / hum_pct / temp_ts / temp_count so the
    ripperdoc board and the twin can show the room.
    """

    def __init__(self, gpio=DEFAULT_TEMP_GPIO, period=DEFAULT_TEMP_PERIOD,
                 chip=None):
        self.gpio = gpio
        self.period = period
        self.chip = chip
        self._stop = threading.Event()
        self._fails = 0
        self._last_pulses = []

    def _collect(self):
        try:
            import gpiod
            from gpiod.line import Direction, Edge, Value
        except ImportError:
            return None
        chips = [self.chip] if self.chip else list(SNR_CHIPS)
        # start signal: drive the line low for 20ms
        req = None
        for path in chips:
            try:
                req = gpiod.request_lines(
                    path, consumer="loa-dht",
                    config={self.gpio: gpiod.LineSettings(
                        direction=Direction.OUTPUT,
                        output_value=Value.INACTIVE)})
                break
            except OSError:
                continue
        if req is None:
            return None
        req.set_value(self.gpio, Value.INACTIVE)
        time.sleep(0.02)
        req.release()
        # sample as input with kernel edge detection — timestamps come from
        # the kernel, immune to GIL jitter from the PIR poller's subprocesses
        req = None
        for path in chips:
            try:
                req = gpiod.request_lines(
                    path, consumer="loa-dht",
                    config={self.gpio: gpiod.LineSettings(
                        direction=Direction.INPUT,
                        edge_detection=Edge.BOTH)})
                break
            except OSError:
                continue
        if req is None:
            return None
        import select
        edges = []
        deadline = time.monotonic() + DHT_PULSE_WINDOW
        while time.monotonic() < deadline and len(edges) < DHT_MAX_PULSES * 2:
            r, _, _ = select.select([req.fd], [], [], 0.005)
            if not r:
                continue
            for ev in req.read_edge_events():
                kind = getattr(ev, "event_type", getattr(ev, "type", None))
                ts = getattr(ev, "timestamp_ns", getattr(ev, "timestamp", None))
                rising = None
                if kind is not None:
                    try:
                        rising = int(kind) == 1  # GPIO_V2_LINE_EVENT_RISING
                    except (TypeError, ValueError):
                        rising = "RISING" in str(kind)
                edges.append((rising, ts))
        req.release()
        # high durations from rising->falling pairs; stray edges are skipped
        highs = []
        i = 0
        while i < len(edges) - 1:
            if edges[i][0]:
                highs.append(edges[i + 1][1] - edges[i][1])
                i += 2
            else:
                i += 1
        return highs

    @staticmethod
    def _decode(pulses):
        # try every start offset; only a checksum-valid frame is accepted
        for s in range(0, max(0, len(pulses) - 39)):
            bits = [1 if p > DHT_ONE_US else 0 for p in pulses[s:s + 40]]
            if len(bits) != 40:
                continue
            b = [int("".join(map(str, bits[i * 8:(i + 1) * 8])), 2)
                 for i in range(5)]
            if ((b[0] + b[1] + b[2] + b[3]) & 0xFF) != b[4]:
                continue
            hum = b[0] + b[1] / 10.0
            temp = b[2] + b[3] / 10.0
            if -40.0 <= temp <= 80.0 and 0.0 <= hum <= 100.0:
                return temp, hum
        return None

    def read(self):
        pulses = self._collect()
        self._last_pulses = pulses or []
        if not pulses:
            return None
        return self._decode(pulses)

    def tick(self):
        v = self.read()
        if v is None:
            self._fails += 1
            if self._fails == 1 or self._fails % 10 == 0:
                pulses = self._last_pulses
                detail = (f"n={len(pulses)} head={pulses[:6]}"
                          if pulses else "no pulses")
                print(f"dht: read failed ({self._fails}x) {detail}",
                      file=sys.stderr, flush=True)
            return
        self._fails = 0
        temp, hum = v
        now = time.time()
        n = (cortex.get_state().get("temp_count") or 0) + 1
        cortex.set_state({"temp_c": temp, "hum_pct": hum,
                          "temp_ts": now, "temp_count": n})

    def run(self):
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self.period)

    def stop(self):
        self._stop.set()


class BMP180:
    """Barometric pressure + onboard temp on I2C (the REMOTE weather board).

    The XC3702 is BMP180-class (chip ID 0x55), NOT a BMP280 — registers
    (0xAA-0xBF cal, 0xF4 ctrl, 0xF6 out) and the compensation differ.
    Stdlib only via the i2c chardev (/dev/i2c-N + I2C_SLAVE) — no smbus.
    Calibration is read once at first use. A missing bus or a dead chip
    returns None instead of hanging the daemon.

    Reads land in cortex as pressure_hpa / baro_temp_c / baro_ts /
    baro_count so the ripperdoc board and the twin can show the air.
    """

    def __init__(self, bus=DEFAULT_BARO_BUS, addr=DEFAULT_BARO_ADDR,
                 period=DEFAULT_BARO_PERIOD, reader=None):
        self.path = f"/dev/i2c-{bus}"
        self.addr = addr
        self.period = period
        self.reader = reader or self._hw_read
        self._stop = threading.Event()
        self._fails = 0
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
        n = (cortex.get_state().get("baro_count") or 0) + 1
        cortex.set_state({"pressure_hpa": pa / 100.0, "baro_temp_c": temp_c,
                          "baro_ts": now, "baro_count": n})

    def run(self):
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self.period)

    def stop(self):
        self._stop.set()


def main():
    cfg = config.load()
    gpio = int(cfg.get("sense_gpio", DEFAULT_GPIO))
    cooldown = float(cfg.get("sense_cooldown", DEFAULT_COOLDOWN))
    trig = int(cfg.get("sense_trig", DEFAULT_TRIG))
    echo = int(cfg.get("sense_echo", DEFAULT_ECHO))
    period = float(cfg.get("sense_period", DEFAULT_SNR_PERIOD))
    snr_enabled = str(cfg.get("sense_snr_enabled", "true")).lower() \
        not in ("0", "false", "no", "off")
    temp_gpio = int(cfg.get("sense_temp_gpio", DEFAULT_TEMP_GPIO))
    temp_period = float(cfg.get("sense_temp_period", DEFAULT_TEMP_PERIOD))
    baro_enabled = str(cfg.get("sense_baro_enabled", "true")).lower() \
        not in ("0", "false", "no", "off")
    baro_addr = int(str(cfg.get("sense_baro_addr", DEFAULT_BARO_ADDR)), 0)
    baro_period = float(cfg.get("sense_baro_period", DEFAULT_BARO_PERIOD))
    # the N counter is per-boot: a rebooted body starts at zero
    cortex.set_state({"sense_count": 0, "snr_count": 0})
    if not snr_enabled:
        # a disabled sonar is silent: no pings, no chirps, no reads
        cortex.set_state({"snr_cm": None, "snr_ts": None})
    cortex.log_event("boot", {"svc": "sense", "gpio": gpio,
                              "cooldown": cooldown, "trig": trig,
                              "echo": echo, "snr_period": period,
                              "snr_enabled": snr_enabled,
                              "temp_gpio": temp_gpio,
                              "temp_period": temp_period,
                              "baro_enabled": baro_enabled,
                              "baro_addr": baro_addr,
                              "baro_period": baro_period})
    set_input(gpio)
    # sync the light with the pin at boot — a stuck/stale state must not
    # survive a reboot (jumper fiddling can leave the module latched high)
    level = pinctrl_reader(gpio)
    if level is not None:
        cortex.set_state({"pir_high": int(level),
                          "pir_on_ts": time.time() if level else None})
    snr = None
    if snr_enabled:
        snr = Sonar(trig=trig, echo=echo, period=period)
        threading.Thread(target=snr.run, daemon=True).start()
    dht = DHT11(gpio=temp_gpio, period=temp_period)
    threading.Thread(target=dht.run, daemon=True).start()
    baro = None
    if baro_enabled:
        baro = BMP180(addr=baro_addr, period=baro_period)
        threading.Thread(target=baro.run, daemon=True).start()
    try:
        SensePoller(gpio=gpio, cooldown=cooldown).run()
    finally:
        if snr is not None:
            snr.stop()
        dht.stop()
        if baro is not None:
            baro.stop()


if __name__ == "__main__":
    main()