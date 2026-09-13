"""loa — the loa outpost control stack.

ONE DIRECTORY PER SUBSYSTEM, so the tree says what each file is:

  * ``topic`` / ``geom`` / ``config`` / ``sense`` — the wire, the numbers, the
    as-built config, and the senses' plumbing. The shared floor: ``sense`` is
    the FRAMEWORK only (publish, the topic claim, the init handshake) — every
    driver lives in the subsystem folder it belongs to.
  * ``cortex/`` — THE BRAIN. ``state`` (RAM state, settings master), ``store``
    (postgres on aleph), ``frames`` (assembles the picture), ``face`` (the
    pages: state -> 1024 bytes), ``ring`` (the ring's frame builder), ``amiga``
    / ``topaz`` (the face's art and font), ``moods`` / ``expressions`` (the
    vocabulary), and ``__main__`` (the /api door, the tick, ingest).
  * ``ring/`` — the LED's VOICE, in the brain: ``encode`` (code space + dither)
    and ``animations`` (the curves the cortex draws). The BLIT LOOP and the
    WS2812 hardware are Rust now (``rust/loa-ring``); what is left here is the
    renderer, which the cortex owns.
  * ``motion/ sonar/ weather/ fault/`` — one process per sense, and the whole
    sense in the one folder: ``__main__`` (its daemon) and its DRIVER — ``pir``
    in motion, ``ultrasonic`` in sonar, ``dht`` + ``baro`` in weather.
  * ``vault/`` — the journal. ``ripperdoc/`` — the console (the client).

THE DISPLAYS ARE NOT HERE ANY MORE. ``oled/`` and ``ring/__main__`` +
``ring/neopixel`` were the Python displays — the blit loop and the SH1106 and
WS2812 drivers — and Rust replaced them (``rust/loa-panel``, ``rust/loa-ring``).
Deleted rather than left as a second way to drive the same glass: a display that
exists in two languages is a display whose two copies drift, which is exactly
what happened to the wire framing on 2026-09-14.

LAZY ON PURPOSE. `import loa` used to drag the whole body in — the renderers,
the cortex, the HTTP door — into EVERY process. Names resolve on first access
now (PEP 562).
"""
import importlib

__version__ = "0.9.0"

#: Every submodule `loa.<name>` that resolves on first access. Kept as a tuple
#: so `from loa import cortex` and `loa.cortex` both work without importing
#: anything until one of them is asked for.
_SUBMODULES = (
    "config", "cortex", "fault", "geom", "motion", "ring", "ripperdoc",
    "sense", "sonar", "topic", "vault", "weather",
)

__all__ = ["__version__", *_SUBMODULES]


def __getattr__(name):
    if name in _SUBMODULES:
        mod = importlib.import_module("." + name, __name__)
        globals()[name] = mod
        return mod
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(__all__) | set(globals()))
