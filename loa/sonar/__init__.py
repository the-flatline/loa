"""sonar — the ultrasonic range daemon (loa-sonar, TRIG 23 / ECHO 22).

  __main__     the daemon  (python -m loa.sonar)
  ultrasonic   the DRIVER — the TRIG/ECHO timing, the gpiod line request

One process per sense. Publishes its readings on the `sonar` topic. The folder
is the whole sense: the driver came here out of `loa/sense.py`, which is now
only the wire.
"""
