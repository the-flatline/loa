"""loa — the loa outpost control stack.

Hardware layers (Ring: WS2812B over SPI; SH1106: the face over SPI0).
Geometry (geom): the frame sizes the wire and the glass share.
Animation layers (pure math, no hardware — render anywhere).
Cortex (cortex): the body's live state; the cortex OWNS the picture.
API (cortexd): the FastAPI door the brain talks to.
Daemons (ring: the ring; oled: the face).

LAZY ON PURPOSE. `import loa` used to drag the whole body in — the renderers,
the cortex, the HTTP door — into EVERY process, which meant a display daemon
that ran `from loa import oled` had physically loaded the renderer it is
forbidden to use. Names resolve on first access now (PEP 562), so a daemon
imports its own layer and nothing else: `loa-oled` reaches panel/geom/topic and
`loa-ring` reaches ws2812/geom/topic, and neither can touch loa/face.py or
loa/cortex.py. tests/test_display_boundary.py asserts that on the real import
graph, in a subprocess, because an eager `from . import face` here would make
the boundary a fiction while every module-level test still passed.
"""
import importlib

__version__ = "0.9.0"

#: Every submodule `loa.<name>` that resolves on first access. Kept as a tuple
#: so `from loa import face` and `loa.face` both work without importing
#: anything until one of them is asked for.
_SUBMODULES = (
    "amiga", "animations", "config", "cortex", "cortexd", "expressions",
    "face", "fault", "frames", "geom", "moods", "motion", "oled", "panel",
    "render", "ring", "ripperdoc", "sense", "sonar", "store", "topaz",
    "topic", "vault", "weather", "ws2812",
)

__all__ = ["Ring", "__version__", *_SUBMODULES]


def __getattr__(name):
    if name == "Ring":
        from .ws2812 import Ring
        globals()["Ring"] = Ring
        return Ring
    if name in _SUBMODULES:
        mod = importlib.import_module("." + name, __name__)
        globals()[name] = mod
        return mod
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(__all__) | set(globals()))
