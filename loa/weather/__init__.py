"""weather — the environment daemon (loa-weather): temp/humidity + baro.

  __main__   the daemon  (python -m loa.weather)
  dht        the DRIVER — the DHT11-class board on GPIO4
  baro       the DRIVER — the BMP180-class baro on I2C 0x77

Both sensors are environment readings from the same remote board, and they
share a process so their failures stay in one place. Publishes the DHT on the
`weather` topic and the baro on `baro`. The folder is the whole sense: the
drivers came here out of `loa/sense.py`, which is now only the wire.
"""
