"""pir — the PIR driver (XC4444 / HC-SR501) on GPIO17.

The motion daemon's hardware, in the motion daemon's folder. It was in
`loa/sense.py` next to the sonar, the DHT and the baro — and a driver that
shares a module with three other sensors shares a module with their failures:
the DHT's blocking pulse-collection sat on the same loop as this poll and the
PIR logged nothing for two hours while it was working fine (2026-09-12). The
split is per-process (one daemon per sense); this is the other half — the
DRIVER lives with the daemon that runs it, so `loa/motion/` is the whole PIR
sense and nothing else has a reason to import into it.

Pin reading is a pinctrl subprocess (works as flatline on the Pi, no sudo).
Off-Pi, tests inject a fake reader. The reading is PUBLISHED on the `pir`
topic; this module never touches the cortex or the store.
"""

import re
import subprocess
import time

from ..sense import publish, publish_event

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
        #: When this driver last saw the pin go high. The driver counts its OWN
        #: motion and times its OWN hold — it used to ask the cortex for the
        #: timestamp it had itself published, a driver reaching into another
        #: service for a number it was the only source of.
        self._on_ts = None

    def _default_fire(self):
        # The driver counts its OWN motion, in memory. It used to ask the cortex
        # for the count — a driver reaching into another service for a number it
        # is itself the only source of.
        now = time.time()
        self._n = getattr(self, "_n", 0) + 1
        n = self._n
        publish({"pir_count": n, "pir_last_ts": now})
        publish_event("sense", {"kind": "pir", "gpio": self.gpio,
                                   "action": "motion", "count": n,
                                   "cooldown": self.cooldown})
        # A scan is a one-shot RECORD now, not a flag written into the cortex:
        # the ring watches the event topic and plays it. Raising it here used
        # to mean this driver writing another service's state.
        publish_event("ring", {"state": "scan"})

    def tick(self):
        level = self.reader(self.gpio)
        if level is None:
            self.assert_input(self.gpio)
            self._pending = 0
            return
        now = time.time()
        if level != self._last:
            if level:
                self._on_ts = now
                publish({"pir_high": 1, "pir_on_ts": now})
            else:
                hold = 0.0 if self._on_ts is None else now - self._on_ts
                self._on_ts = None
                publish({"pir_high": 0, "pir_last_hold": hold,
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
