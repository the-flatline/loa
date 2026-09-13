"""sonar — the ultrasonic range daemon (loa-sonar).

Own process because the echo wait is a timed blocking read: if the module is
absent the measurement sits on a timeout, and that must not be time taken from
the PIR.
"""
import time

from .. import config
from ..sense import publish, publish_event, subscribe_init, use_topic
from .ultrasonic import (DEFAULT_ECHO, DEFAULT_SNR_PERIOD, DEFAULT_TRIG,
                         Sonar)


def main():
    cfg = config.load()
    trig = int(cfg.get("sense_trig", DEFAULT_TRIG))
    echo = int(cfg.get("sense_echo", DEFAULT_ECHO))
    period = float(cfg.get("sense_period", DEFAULT_SNR_PERIOD))
    enabled = str(cfg.get("sense_snr_enabled", "true")).lower() \
        not in ("0", "false", "no", "off")
    use_topic("sonar")
    # The init handshake: a restarted cortex has forgotten the last range, and
    # the sonar is change-only — it must be ASKED for its full payload.
    subscribe_init()

    publish({"snr_count": 0})
    if not enabled:
        # A disabled sonar is silent: no pings, no chirps, no reads. Stay up
        # rather than exiting — the unit is Restart=always, so exiting here
        # would spin a restart loop for a sensor that is deliberately off.
        publish({"snr_cm": None, "snr_ts": None})
        publish_event("boot", {"svc": "sonar", "enabled": False})
        while True:
            time.sleep(3600)
    publish_event("boot", {"svc": "sonar", "trig": trig, "echo": echo,
                           "period": period})
    Sonar(trig=trig, echo=echo, period=period).run()


if __name__ == "__main__":
    main()
