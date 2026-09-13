"""weather — the environment daemon (loa-weather): temp/humidity + baro.

Both sensors are environment readings from the same remote board, and both
answer the same question: what is it like where the body is? They share a
process so their failures stay in one place, away from the senses that matter
for reacting to a person.

The DHT read is a blocking pulse-collection with a hard window, and the baro
read is an i2c transaction with a measurement wait. Neither may take time from
the PIR — that is why they are here and not in loa-motion.
"""
import threading
import time

from . import config
from .sense import (BMP180, DEFAULT_BARO_ADDR, DEFAULT_BARO_PERIOD,
                    DEFAULT_TEMP_GPIO, DEFAULT_TEMP_PERIOD, DHT11,
                    publish_event, subscribe_init, use_topic)


def main():
    cfg = config.load()
    temp_gpio = int(cfg.get("sense_temp_gpio", DEFAULT_TEMP_GPIO))
    temp_period = float(cfg.get("sense_temp_period", DEFAULT_TEMP_PERIOD))
    baro_addr = int(str(cfg.get("sense_baro_addr", DEFAULT_BARO_ADDR)), 0)
    baro_period = float(cfg.get("sense_baro_period", DEFAULT_BARO_PERIOD))
    baro_enabled = str(cfg.get("sense_baro_enabled", "true")).lower() \
        not in ("0", "false", "no", "off")
    use_topic("weather")
    # The init handshake. This daemon hosts TWO sources (weather + baro), so
    # the answer re-sends each under its own name — the same split the readings
    # arrive under.
    subscribe_init()

    publish_event("boot", {"svc": "weather", "temp_gpio": temp_gpio,
                              "temp_period": temp_period,
                              "baro_enabled": baro_enabled,
                              "baro_addr": baro_addr,
                              "baro_period": baro_period})

    dht = DHT11(gpio=temp_gpio, period=temp_period)
    threading.Thread(target=dht.run, daemon=True).start()
    baro = None
    if baro_enabled:
        baro = BMP180(addr=baro_addr, period=baro_period)
        threading.Thread(target=baro.run, daemon=True).start()

    try:
        # the sensor threads do the work; this one holds the process open.
        while True:
            time.sleep(3600)
    finally:
        dht.stop()
        if baro is not None:
            baro.stop()


if __name__ == "__main__":
    main()
