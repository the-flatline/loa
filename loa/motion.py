"""motion — the PIR daemon (loa-motion).

The point of this being its own process: it used to share one with the DHT and
the baro, and those are the two that fail. `dht: read failed (10x) no pulses`
was logged every ~100s in the same loop as the motion poll, and the DHT read is
a blocking pulse-collection. A sensor that hangs while holding the loop deafens
everything else in it — found live 2026-09-12, two hours of silence from a PIR
that was working the whole time.

One process per sense means a dead weather board cannot blind the eye.
"""
import time

from . import config
from . import cortex
from .sense import (DEFAULT_COOLDOWN, DEFAULT_GPIO, SensePoller, pinctrl_reader,
                    set_input)


def main():
    cfg = config.load()
    gpio = int(cfg.get("sense_gpio", DEFAULT_GPIO))
    cooldown = float(cfg.get("sense_cooldown", DEFAULT_COOLDOWN))

    # the N counter is per-boot: a rebooted body starts at zero
    cortex.set_state({"sense_count": 0})
    cortex.log_event("boot", {"svc": "motion", "gpio": gpio,
                              "cooldown": cooldown})
    set_input(gpio)
    # sync the light with the pin at boot — a stuck/stale state must not
    # survive a reboot (jumper fiddling can leave the module latched high)
    level = pinctrl_reader(gpio)
    if level is not None:
        cortex.set_state({"pir_high": int(level),
                          "pir_on_ts": time.time() if level else None})
    SensePoller(gpio=gpio, cooldown=cooldown).run()


if __name__ == "__main__":
    main()
