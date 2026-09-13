"""motion — the PIR daemon (loa-motion, GPIO17).

  __main__   the daemon  (python -m loa.motion)
  pir        the DRIVER — the pin, the debounce, the cooldown

One process per sense so a hung sensor cannot deafen the eye. Publishes its
readings on the `pir` topic. The folder is the whole sense: the driver came
here out of `loa/sense.py`, which is now only the wire.
"""
