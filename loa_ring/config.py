"""config — loa.conf reader. Simple key=value file, ~/loa.conf by default.

Environment variables override file keys (LOA_RING_BUS, LOA_OLED_BUS, ...).

The as-built truth (2026-09-07, pinctrl-verified): the ring lives on SPI1,
MOSI = P38/GPIO20, /dev/spidev1.0 — OPPOSITE of the old notes. The OLED is
on SPI0 CE0. loa.conf on the Pi records that; this reader keeps the library
aligned with the bench without hardcoding pins into code.
"""

import os

DEFAULT_PATH = os.path.expanduser("~/loa.conf")

ENV_KEYS = {
    "ring_bus": "LOA_RING_BUS",
    "ring_device": "LOA_RING_DEVICE",
    "ring_speed": "LOA_RING_SPEED",
    "oled_bus": "LOA_OLED_BUS",
    "oled_device": "LOA_OLED_DEVICE",
    "oled_speed": "LOA_OLED_SPEED",
    "oled_offset": "LOA_OLED_OFFSET",
    "cortex_db": "LOA_CORTEX_DB",
}


def load(path=None):
    """Return a dict of loa.conf keys; env vars win. Missing file -> {}."""
    cfg = {}
    p = path or DEFAULT_PATH
    try:
        with open(p) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                cfg[k.strip().lower()] = v.strip()
    except OSError:
        pass
    for k, env in ENV_KEYS.items():
        if os.environ.get(env):
            cfg[k] = os.environ[env]
    return cfg