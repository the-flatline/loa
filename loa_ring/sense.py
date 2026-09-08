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
import time

from . import config
from . import cortex
from . import moods

POLL_PERIOD = 0.2
DEFAULT_GPIO = 17
DEFAULT_COOLDOWN = 5.0

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


def main():
    cfg = config.load()
    gpio = int(cfg.get("sense_gpio", DEFAULT_GPIO))
    cooldown = float(cfg.get("sense_cooldown", DEFAULT_COOLDOWN))
    cortex.log_event("boot", {"svc": "sense", "gpio": gpio,
                              "cooldown": cooldown})
    set_input(gpio)
    SensePoller(gpio=gpio, cooldown=cooldown).run()


if __name__ == "__main__":
    main()