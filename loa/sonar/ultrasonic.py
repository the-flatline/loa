"""ultrasonic — the ultrasonic ranger driver (XC4442 / HC-SR04), TRIG 23 / ECHO 22.

The sonar daemon's hardware, in the sonar daemon's folder. TRIG pulse on one
GPIO, time the ECHO high pulse on another: needs microsecond timing, so gpiod
(not pinctrl) drives it; off-Pi tests inject a fake measure().

Reads are PUBLISHED on the `sonar` topic as snr_cm / snr_ts / snr_count so the
ripperdoc board can show the LAST distance — the echo pulse is too short to see
as a light, the number is the signal. Nothing here touches the cortex: the
driver holds its own count.
"""

import threading
import time

from ..sense import publish

DEFAULT_TRIG = 23
DEFAULT_ECHO = 22
DEFAULT_SNR_PERIOD = 1.0
SNR_TIMEOUT = 0.03           # 30ms echo wait — ~500cm ceiling
#: The box's gpiochips, in order. The line lives on one of these and which one
#: is a property of the board, not of the daemon — so each driver that drives a
#: GPIO line carries the list itself and its folder stays whole.
GPIO_CHIPS = ("/dev/gpiochip0", "/dev/gpiochip4")


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
        self._count = 0

    def _gpiod_measure(self):
        try:
            import gpiod
            from gpiod.line import Direction, Value
        except ImportError:
            return None
        for path in ([self.chip] if self.chip else list(GPIO_CHIPS)):
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
        # The driver holds its own count. Reaching into the cortex's state for a
        # counter is the same cross-service read as reaching for the reading,
        # and the count is per-boot anyway: a rebooted body starts at zero.
        self._count += 1
        publish({"snr_cm": cm, "snr_ts": now, "snr_count": self._count})

    def run(self):
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self.period)

    def stop(self):
        self._stop.set()
