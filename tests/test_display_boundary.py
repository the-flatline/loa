"""The display cannot reach the renderer — structurally, not by convention.

`loa-oled` ran `from loa import face`, and `loa-ring` ran `from loa import
frames`, because face.py held the SH1106 DRIVER next to the face RENDERER and
the ring daemon wanted one number out of the frame module. Both worked. Both
meant the display process had physically loaded the rendering code and the
state derivation that lives with it (`seal_state`, `faults_state`,
`power_status` in face.py) — so "the display only reads the dumb functions" was
a promise about code that nothing stopped from changing, and the first import
added for convenience would have made it a false one.

Split 2026-09-13: the driver is `loa/panel.py`, the frame geometry is
`loa/geom.py`, and a display daemon's imports are exactly
{its driver, the geometry, the topic}. THIS FILE is what keeps it that way. The
rule is asserted twice on purpose:

  * over the SOURCE (AST) — every `from . import x` in the display's closure,
    module level or inside a function, because a lazy import is still an import;
  * over the REAL import graph — `loa.oled`/`loa.ring` imported in a subprocess,
    because `loa/__init__.py` re-exporting a renderer would satisfy any
    module-level test while putting the renderer back in the daemon's process.

It is the same rule as the one-process-per-sense split in test_split_daemons.py:
the boundary has to be a property of the code, not a comment asking politely.
"""
import ast
import json
import os
import pathlib
import subprocess
import sys

import pytest

from loa import animations, face, frames, geom, oled, panel, ring

ROOT = pathlib.Path(__file__).resolve().parent.parent
PKG = ROOT / "loa"

#: What a PURE DISPLAY may hold, beyond stdlib: its own DRIVER, the frame
#: GEOMETRY and the TOPIC. `config` rides with the driver — reading its own
#: bus/offset out of loa.conf is the driver's business, and the driver is the
#: only thing that does it. `pb` is the generated protobuf binding the topic
#: module needs, carrying no loa code of its own.
ALLOWED = {
    "oled": {"oled", "panel", "geom", "topic", "config", "pb"},
    "ring": {"ring", "ws2812", "geom", "topic", "config", "pb"},
}

#: The rendering code and the state behind it. Named individually so a failure
#: says WHAT leaked, not just that something did.
FORBIDDEN = ("face", "frames", "render", "animations", "amiga", "topaz",
             "moods", "expressions", "cortex", "cortexd", "store", "vault",
             "ripperdoc", "fault", "sense", "motion", "sonar", "weather")


# ---------------------------------------------------------------------------
# 1. the boundary, over the source

def _imports_in(src: str) -> set[str]:
    """Every loa submodule a source imports — module level OR inside a function.

    A lazy `from . import config` in an `__init__` is still a dependency: if the
    driver may not have it, the display may not reach it either.
    """
    found: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            for a in node.names:
                parts = a.name.split(".")
                if parts[0] == "loa" and len(parts) > 1:
                    found.add(parts[1])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module and node.module.split(".")[0] == "loa":
                    parts = node.module.split(".")
                    found.add(parts[1] if len(parts) > 1 else "")
            elif node.level == 1:
                if node.module:                 # from .panel import X
                    found.add(node.module.split(".")[0])
                else:                           # from . import x, y
                    found.update(a.name for a in node.names)
    return {m for m in found if m}


def _closure(mod: str, seen: set[str] | None = None) -> set[str]:
    """The loa submodules `mod` transitively imports, from the source."""
    seen = set() if seen is None else seen
    for name in _imports_in((PKG / f"{mod}.py").read_text()):
        if name in seen or not (PKG / f"{name}.py").exists():
            seen.add(name)
            continue
        seen.add(name)
        _closure(name, seen)
    return seen


@pytest.mark.parametrize("display", ["oled", "ring"])
def test_a_display_imports_only_its_driver_the_geometry_and_the_topic(display):
    got = _closure(display)
    extra = got - ALLOWED[display]
    assert not extra, (
        f"{display}.py reaches {sorted(extra)} — a display's imports are "
        f"exactly its driver, the frame geometry and the topic")


@pytest.mark.parametrize("display", ["oled", "ring"])
def test_a_display_cannot_reach_the_renderer(display):
    """The thing itself, stated as its own failure: this is what the split was
    for, and the message has to name the module that leaked."""
    leaks = _closure(display) & set(FORBIDDEN)
    assert not leaks, (
        f"{display}.py can reach the renderer/state {sorted(leaks)} — the "
        f"display process must not be able to draw or to derive state")


def test_the_boundary_is_not_vacuous():
    """`_closure` must actually see imports, or every assertion above passes on
    an empty set and the guard is decoration."""
    assert _closure("oled") >= {"panel", "geom", "topic"}
    assert _closure("ring") >= {"ws2812", "geom", "topic"}
    assert _closure("frames") & set(FORBIDDEN), (
        "the renderer itself must show up as reaching rendering code")


def test_the_display_modules_do_not_even_name_the_renderer():
    for name in ("oled", "ring"):
        src = (PKG / f"{name}.py").read_text()
        body = "\n".join(line for line in src.splitlines()
                         if not line.lstrip().startswith("#"))
        assert "import face" not in body and "import frames" not in body, (
            f"{name}.py imports the renderer")


# ---------------------------------------------------------------------------
# 2. the boundary, over the real import graph

def _loa_modules_loaded_by(module: str) -> set[str]:
    """Import the display in a subprocess and list the loa modules that came
    with it. Source analysis cannot see `loa/__init__.py`: if the package
    re-exports a renderer, the daemon loads it no matter what the daemon's own
    imports say."""
    script = (
        "import json, sys\n"
        f"import loa.{module}\n"
        "print(json.dumps(sorted(m for m in sys.modules"
        " if m.split('.')[0] == 'loa')))"
    )
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    r = subprocess.run([sys.executable, "-c", script], capture_output=True,
                       text=True, env=env, cwd=str(ROOT), timeout=60)
    assert r.returncode == 0, f"importing loa.{module} failed:\n{r.stderr}"
    return set(json.loads(r.stdout.strip().splitlines()[-1]))


@pytest.mark.parametrize("display,driver", [("oled", "panel"), ("ring", "ws2812")])
def test_the_running_display_process_never_loads_the_renderer(display, driver):
    loaded = _loa_modules_loaded_by(display)
    assert f"loa.{driver}" in loaded, "the driver did not load — test is vacuous"
    leaks = {m for m in loaded if m.removeprefix("loa.") in FORBIDDEN}
    assert not leaks, (
        f"importing loa.{display} loaded the renderer/state {sorted(leaks)} — "
        f"loa/__init__.py must stay lazy, or the display process holds the code "
        f"it is not allowed to reach")


def test_the_package_no_longer_drags_the_body_into_every_process():
    """`import loa` is the package every daemon pays for. It may carry the
    version and nothing else."""
    script = ("import json, sys\nimport loa\n"
              "print(json.dumps(sorted(m for m in sys.modules"
              " if m.split('.')[0] == 'loa')))")
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    r = subprocess.run([sys.executable, "-c", script], capture_output=True,
                       text=True, env=env, cwd=str(ROOT), timeout=60)
    assert r.returncode == 0, r.stderr
    loaded = set(json.loads(r.stdout.strip().splitlines()[-1]))
    assert loaded == {"loa"}, f"`import loa` pulled in {sorted(loaded)}"


# ---------------------------------------------------------------------------
# 3. the split lost no name and kept one number

def test_face_still_answers_to_every_name_it_used_to():
    """The renderer's callers say `face.WIDTH`, `face.NullDisplay`. The names
    survived the split — the module boundary is what changed."""
    assert face.WIDTH == panel.WIDTH == geom.FACE_WIDTH
    assert face.HEIGHT == panel.HEIGHT == geom.FACE_HEIGHT
    assert face.PAGES == panel.PAGES == geom.FACE_PAGES
    assert face.DEFAULT_FLIP == panel.DEFAULT_FLIP
    assert face.get_display is panel.get_display
    assert face.NullDisplay is panel.NullDisplay
    assert face.SH1106 is panel.SH1106


def test_the_wire_and_the_glass_share_one_number():
    """128x64 1bpp page-major is 1024 bytes and 24 px RGB is 72 — on the feed AND
    on the panel. Both sides read geom, so neither can drift."""
    assert geom.FACE_BYTES == 1024
    assert geom.RING_BYTES == 72
    assert frames.FACE_BYTES == geom.FACE_BYTES
    assert frames.RING_BYTES == geom.RING_BYTES
    assert animations.LED_COUNT == geom.RING_LEDS
    assert panel.WIDTH * panel.PAGES == geom.FACE_BYTES


# ---------------------------------------------------------------------------
# 4. the glass shows the PUBLISHED bytes

class _Glass:
    """A panel that records exactly what it was told to draw."""

    def __init__(self):
        self.shown: list[bytes] = []
        self.flips: list[bool] = []
        self.contrasts: list[int] = []
        self.cleared = 0

    def show(self, buf, offset=None):
        self.shown.append(bytes(buf))

    def set_flip(self, flipped):
        self.flips.append(bool(flipped))

    def set_contrast(self, val):
        self.contrasts.append(int(val))

    def clear(self):
        self.cleared += 1

    def close(self):
        pass


@pytest.fixture
def glass():
    """A panel and a display with no memory of the last frame — the module
    remembers what it last blitted, so a test must start it blank."""
    oled._LAST["buf"] = None
    oled._LAST["contrast"] = None
    oled._FLIP["on"] = None
    yield _Glass()
    oled._LAST["buf"] = None
    oled._LAST["contrast"] = None
    oled._FLIP["on"] = None


def _msg(frame: bytes, **kw):
    from loa.pb import loa_pb2
    return loa_pb2.Ripperdoc(face=frame, **kw)


def test_the_glass_shows_the_bytes_the_cortex_published(glass):
    frame = bytes(range(256)) * 4           # 1024 B, every byte distinct
    assert oled._blit(glass, _msg(frame, oled_flip=False, oled_dim=False)) is True
    assert glass.shown == [frame], (
        "the display did not blit exactly the published bytes — it must draw "
        "the frame it was handed, not a frame of its own")


def test_orientation_and_contrast_go_to_the_panel_not_into_the_bytes(glass):
    """The image on the wire stays canonical: a byte-level flip turns the glass
    right way up and leaves every feed consumer drawing it upside down."""
    frame = bytes(range(256)) * 4
    oled._blit(glass, _msg(frame, oled_flip=True, oled_dim=True))
    assert glass.flips == [True]
    assert glass.contrasts == [oled.DIM]
    assert glass.shown[-1] == frame, "the bytes were transformed, not the panel"


def test_a_frame_that_is_not_the_geometry_is_refused(glass):
    assert oled._blit(glass, _msg(b"\x00" * 10)) is False
    assert glass.shown == [], "a truncated frame reached the glass"


def test_the_same_picture_twice_is_not_redrawn(glass):
    frame = bytes(range(256)) * 4
    assert oled._blit(glass, _msg(frame)) is True
    assert oled._blit(glass, _msg(frame)) is False
    assert len(glass.shown) == 1


def test_main_clears_the_panel_and_blits_what_the_topic_carries(monkeypatch,
                                                                glass):
    """End to end, with the driver stubbed: the daemon blanks the glass at
    start (a panel holding the last process's frozen frame lies about being
    alive) and then draws the bytes off the topic — from the DRIVER it imports,
    with no renderer in the process."""
    frame = bytes(range(256)) * 4
    waiting = [_msg(frame, oled_flip=False, oled_dim=False)]

    class _Stop(Exception):
        pass

    class _Mirror:
        def __init__(self, topics):
            self.topics = topics

        def message(self, topic):
            if waiting:
                return waiting.pop()
            raise _Stop                       # out of the daemon's loop

        def close(self):
            pass

    monkeypatch.setattr(oled.panel_mod, "get_display", lambda: glass)
    monkeypatch.setattr(oled.topic_mod, "Mirror", _Mirror)
    monkeypatch.setattr(oled.time, "sleep", lambda _s: None)

    with pytest.raises(_Stop):
        oled.main()

    assert glass.cleared == 1, "the panel was not blanked at start"
    assert glass.shown == [frame], "the glass did not show the published bytes"
