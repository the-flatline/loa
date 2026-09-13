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

from .. import config
from ..sense import (DEFAULT_COOLDOWN, DEFAULT_GPIO, SensePoller, publish,
                    publish_event, pinctrl_reader, set_input, subscribe_init,
                    use_topic)


def main():
    cfg = config.load()
    gpio = int(cfg.get("sense_gpio", DEFAULT_GPIO))
    cooldown = float(cfg.get("sense_cooldown", DEFAULT_COOLDOWN))
    # readings go OUT on the topic, not into the database: five processes
    # writing one sqlite file is a race, and the cortex cannot publish what it
    # never sees.
    use_topic("pir")
    # The init handshake: a restarted cortex asks, and this daemon answers with
    # everything it has — the counters, the pin level, the hold — rather than
    # waiting for the pin to move before the brain learns it exists.
    subscribe_init()

    # the N counter is per-boot: a rebooted body starts at zero
    publish({"pir_count": 0})
    # ...and this is the FULL payload on our own start, the other half of the
    # handshake: whenever this process comes up, the cortex hears the whole pin
    # state rather than a change against a state it does not have.
    publish_event("boot", {"svc": "motion", "gpio": gpio,
                           "cooldown": cooldown})
    set_input(gpio)
    # sync the light with the pin at boot — a stuck/stale state must not
    # survive a reboot (jumper fiddling can leave the module latched high)
    level = pinctrl_reader(gpio)
    if level is not None:
        publish({"pir_high": int(level),
                 "pir_on_ts": time.time() if level else None})
    SensePoller(gpio=gpio, cooldown=cooldown).run()


if __name__ == "__main__":
    main()
