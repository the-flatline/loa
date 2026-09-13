"""loa — the loa outpost control stack.

ONE DIRECTORY PER SUBSYSTEM, so the tree says what each file is:

  * ``topic`` / ``geom`` / ``config`` / ``sense`` — the wire, the numbers, the
    as-built config, and the senses' plumbing. The shared floor.
  * ``cortex/`` — THE BRAIN. ``state`` (RAM state, settings master), ``store``
    (postgres on aleph), ``frames`` (assembles the picture), ``face`` (the
    pages: state -> 1024 bytes), ``ring`` (the ring's frame builder), ``amiga``
    / ``topaz`` (the face's art and font), ``moods`` / ``expressions`` (the
    vocabulary), and ``__main__`` (the /api door, the tick, ingest).
  * ``oled/`` — THE GLASS, a limb: ``__main__`` (the blit loop) and ``driver``
    (the SH1106).
  * ``ring/`` — THE LEDS, a limb: ``__main__`` (the blit loop), ``neopixel``
    (the hardware), ``encode`` (code space + dither), ``animations`` (the
    voice).
  * ``motion/ sonar/ weather/ fault/`` — one process per sense, each with its
    own daemon in ``__main__``.
  * ``vault/`` — the journal. ``ripperdoc/`` — the console (the client).

LAZY ON PURPOSE. `import loa` used to drag the whole body in — the renderers,
the cortex, the HTTP door — into EVERY process, which meant a display daemon
that ran `from loa import oled` had physically loaded the renderer it is
forbidden to use. Names resolve on first access now (PEP 562), so a daemon
imports its own layer and nothing else: `loa-oled` reaches oled/driver, geom and
topic and `loa-ring` reaches ring/neopixel, geom and topic, and neither can touch
loa/cortex/face.py or the state derivation beside it.
tests/test_display_boundary.py asserts that on the real import graph, in a
subprocess, because an eager `from . import cortex` here would make the boundary
a fiction while every module-level test still passed.
"""
import importlib

__version__ = "0.9.0"

#: Every submodule `loa.<name>` that resolves on first access. Kept as a tuple
#: so `from loa import cortex` and `loa.cortex` both work without importing
#: anything until one of them is asked for.
_SUBMODULES = (
    "config", "cortex", "fault", "geom", "motion", "oled", "ring", "ripperdoc",
    "sense", "sonar", "topic", "vault", "weather",
)

__all__ = ["Ring", "__version__", *_SUBMODULES]


def __getattr__(name):
    if name == "Ring":
        from .ring.neopixel import Ring
        globals()["Ring"] = Ring
        return Ring
    if name in _SUBMODULES:
        mod = importlib.import_module("." + name, __name__)
        globals()[name] = mod
        return mod
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(__all__) | set(globals()))
