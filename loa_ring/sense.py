"""sense — the input daemon (loa-sense). Owns the senses: PIR on GPIO17
first, then the sonar and the weather board as they land.

The ring breathes as the body's presence display (loa-presence). This is the
other direction — the body noticing the room. On motion it fires the one-shot
scan event (a comet lap) and logs a 'sense' event to the cortex, so the brain
can see what the body felt.

Pin reading is a pinctrl subprocess (works as flatline on the Pi, no sudo).
Off-Pi, tests inject a fake reader.
"""

import re
import subprocess
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


def main():
    cfg = config.load()
    gpio = int(cfg.get("sense_gpio", DEFAULT_GPIO))
    cooldown = float(cfg.get("sense_cooldown", DEFAULT_COOLDOWN))
    trig = int(cfg.get("sense_trig", DEFAULT_TRIG))
    echo = int(cfg.get("sense_echo", DEFAULT_ECHO))
    period = float(cfg.get("sense_period", DEFAULT_SNR_PERIOD))
    # the N counter is per-boot: a rebooted body starts at zero
    cortex.set_state({"sense_count": 0, "snr_count": 0})
    cortex.log_event("boot", {"svc": "sense", "gpio": gpio,
                              "cooldown": cooldown, "trig": trig,
                              "echo": echo, "snr_period": period})
    set_input(gpio)
    # sync the light with the pin at boot — a stuck/stale state must not
    # survive a reboot (jumper fiddling can leave the module latched high)
    level = pinctrl_reader(gpio)
    if level is not None:
        cortex.set_state({"pir_high": int(level),
                          "pir_on_ts": time.time() if level else None})
    snr = Sonar(trig=trig, echo=echo, period=period)
    t = threading.Thread(target=snr.run, daemon=True)
    t.start()
    try:
        SensePoller(gpio=gpio, cooldown=cooldown).run()
    finally:
        snr.stop()


if __name__ == "__main__":
    main()