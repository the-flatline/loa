"""config — loa.conf reader. Handles both JSON and key=value files.

The as-built truth (2026-09-07, pinctrl-verified): the ring lives on SPI1,
MOSI = P38/GPIO20, /dev/spidev1.0 — OPPOSITE of the old notes. The OLED is
on SPI0 CE0. loa.conf on the Pi records that; this reader keeps the library
aligned with the bench without hardcoding pins into code.

The Pi's loa.conf is JSON ({"ring": {...}, "oled": {...}}); older boxes may
use key=value lines. Both parse to the same flat dict:
  ring_bus, ring_device, ring_speed, oled_bus, oled_device, oled_speed,
  oled_offset, cortex_db

Environment variables (LOA_RING_BUS, ...) override file keys.
"""

import json
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

# groups in the JSON form map to these flat keys
_GROUP_KEYS = {
    "ring": ("num", "bus", "device", "speed"),
    "oled": ("bus", "device", "speed", "offset"),
}


def _flatten(data):
    """{"ring": {"bus": 1, ...}, "oled": {...}} -> {"ring_bus": 1, ...}"""
    flat = {}
    for name, keys in _GROUP_KEYS.items():
        group = data.get(name)
        if isinstance(group, dict):
            for k in keys:
                if k in group:
                    flat[f"{name}_{k}"] = group[k]
    return flat


def load(path=None):
    """Return a flat dict of loa.conf keys; env vars win. Missing file -> {}."""
    cfg = {}
    p = path or DEFAULT_PATH
    try:
        with open(p) as f:
            text = f.read()
    except OSError:
        text = None
    if text is not None:
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                cfg.update(_flatten(data))
        except ValueError:
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                cfg[k.strip().lower()] = v.strip()
    for k, env in ENV_KEYS.items():
        if os.environ.get(env):
            cfg[k] = os.environ[env]
    return cfg