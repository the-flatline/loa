"""One process per sense, and the whole sense in one folder.

Why the split exists, as a test rather than a comment: loa-sense ran the PIR,
the sonar, the DHT and the baro in one process, and the DHT read is a blocking
pulse-collection. Found live 2026-09-12 — `dht: read failed (10x) no pulses`
every ~100s, and the PIR silent for two hours while it was working fine.

The process split came first (one daemon per sense). This is the other half:
the DRIVERS came out of `loa/sense.py` — 687 lines that held the wire AND all
four sensors — into the folder that owns each one, and the shared module now
keeps only the framework. The tree says what each file is:

  motion/pir.py       the PIR (was SensePoller + the pinctrl readers)
                          (the range is Rust now: rust/loa-sonar)
  weather/dht.py      the DHT board (was DHT11)
  weather/baro.py     the baro (was BMP180)

So: the PIR must not share a thread with anything that can hang, no daemon may
reach into another subsystem for its driver, and the split must not quietly
lose a setting on the way.
"""
import ast
import importlib
import inspect
import pathlib
import re
import subprocess
import sys

from loa.weather import __main__ as weather

ROOT = pathlib.Path(__file__).resolve().parent.parent / "loa"

#: (folder, driver module, the names the driver owns). The names are the point:
#: `loa.sense` answering for any of them means the driver never really moved.
DRIVERS = (
    ("weather", "dht", ("DHT11",)),
    ("weather", "baro", ("BMP180",)),
)
#: The files each daemon is allowed to load: its own folder, the framework
#: (`loa.sense`) and the shared floor (`loa.topic`, `loa.config`, `loa.geom`).
DAEMONS = (("weather", ("dht", "baro")),)


RUST = pathlib.Path(__file__).resolve().parent.parent / "rust"


def _rust_cfg_keys():
    """The `sense_*` settings the RUST daemons read.

    `setting("LOA_SENSE_GPIO", "sense", "gpio", ..)` names the same key the
    Python reader calls `sense_gpio` — the group_key convention, shared. Without
    this, porting a daemon would look exactly like dropping its settings: the
    test would fail on a setting that is read, just not in Python.
    """
    found = set()
    for manifest in sorted(RUST.glob("loa-*/src/*.rs")):
        text = manifest.read_text()
        for group, key in re.findall(
                r'setting\w*\("LOA_[A-Z_]+",\s*"([a-z_]+)",\s*"([a-z_]+)"', text):
            found.add(f"{group}_{key}")
    return found


def _cfg_keys(module):
    return set(re.findall(r'cfg\.get\("([a-z_]+)"', inspect.getsource(module)))


def test_each_sense_has_its_own_entrypoint():
    for mod in (weather,):
        assert callable(mod.main), f"{mod.__name__} has no main()"


def test_the_split_lost_no_setting():
    """Every `sense_*` key the box can carry must still be read by one of the
    three daemons. A dropped key is a sensor that works in tests and not on the
    body — the failure mode is silence, so nothing would catch it.

    The list is the CANONICAL one (config's own `sense` group), not the retired
    combined daemon: that module is gone, and a test that keeps reading
    `loa.sense` compares against the framework, which reads no config at all —
    an empty set, and a green test over a fully dropped setting.
    """
    from loa import config

    expected = {f"sense_{k}" for k in config._GROUP_KEYS["sense"]}
    read = _cfg_keys(weather) | _rust_cfg_keys()
    lost = expected - read
    assert not lost, f"the split dropped these settings: {sorted(lost)}"


def _identifiers(path):
    """Every name the file's CODE can mean: identifiers, attributes and imports.

    Parsed, not grepped: `motion/__init__.py` explains WHY the DHT is not here
    ("the DHT read is a blocking pulse-collection"), and a check that matches
    that sentence fails on the docstring that documents the rule. A blocking
    read needs an import and a call — those are names.
    """
    tree = ast.parse(path.read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(a.name for a in node.names)
    return names


def test_no_sense_can_be_starved_by_another_sense():
    """The whole point, and it is STRUCTURAL now.

    The PIR went silent for two hours because the DHT's blocking pulse
    collection shared its loop. In Python the guarantee was a check over the
    motion folder; in Rust each sense is a separate BINARY, so a hung sensor
    cannot reach another one at all — there is no shared loop to hang.

    What can still regress is a dependency: if `loa-sonar` ever links
    `loa-motion`, the two are one program again in every way that matters.
    """
    senses = ("loa-motion", "loa-sonar", "loa-weather", "loa-fault")
    for name in senses:
        manifest = RUST / name / "Cargo.toml"
        if not manifest.exists():
            continue                       # not ported yet: still Python
        deps = manifest.read_text()
        for other in senses:
            if other == name:
                continue
            assert other not in deps.replace(name, ""), (
                f"{name} depends on {other}: one sense cannot wait on another")


def test_the_face_says_what_the_pin_says():
    """Motion is only useful if the PIR's own state is published for the face
    and the sweep — and the daemon is RUST now, so the check reads `high`, the
    wire field behind the state key `pir_high`.

    Two things must stay true: it publishes a LEVEL (not only an event), and it
    claims the pin as an input rather than reading a pin nobody configured.
    """
    src = (RUST / "loa-motion" / "src" / "main.rs").read_text()
    assert "high: Some(" in src, "the PIR daemon must publish the pin level"
    assert "claim_input" in src, "the PIR daemon must claim the pin as an input"


def test_the_shared_module_no_longer_answers_for_the_drivers():
    """`loa/sense.py` is the FRAMEWORK: the wire, the topic claim, the init
    handshake. A driver still importable from it is a driver that never moved —
    a re-export is a shim, and code reads the shim, not the folder.

    Both directions, because either half alone is satisfiable by an accident:
    the framework must not answer for a driver, and each driver must be where
    its folder says it is.
    """
    import loa.sense as sense

    for folder, mod, names in DRIVERS:
        for name in names:
            assert not hasattr(sense, name), (
                f"{name} is still answered by loa.sense — it belongs in "
                f"loa/{folder}/{mod}.py")
    assert not hasattr(sense, "main"), (
        "the retired combined daemon (loa.sense.main) is still here; the three "
        "split daemons own the processes")
    for folder, mod, names in DRIVERS:
        where = importlib.import_module(f"loa.{folder}.{mod}")
        for name in names:
            assert hasattr(where, name), (
                f"{name} does not live in loa/{folder}/{mod}.py")


def test_each_daemon_reaches_only_its_own_folder():
    """The folders are self-sufficient: a daemon loads its own driver plus the
    framework, and nothing from another subsystem.

    Read off the real import graph in a SUBPROCESS (sys.modules after the daemon
    module is imported), not off the source: an attribute grep sees the import
    line and not what it resolves to, and a driver pulled in two hops away is
    exactly the coupling this catches.
    """
    for folder, drivers in DAEMONS:
        drivers = (drivers,) if isinstance(drivers, str) else drivers
        others = [d for d, _ in DAEMONS if d != folder]
        code = (f"import sys, loa.{folder}.__main__; "
                "print(' '.join(sorted(m for m in sys.modules if m.startswith('loa'))))")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, cwd=str(ROOT.parent))
        assert out.returncode == 0, f"loa.{folder}.__main__ will not import:\n{out.stderr}"
        loaded = set(out.stdout.split())
        for driver in drivers:
            assert f"loa.{folder}.{driver}" in loaded, (
                f"{folder} does not load its own driver loa/{folder}/{driver}.py")
        for other in others:
            reached = sorted(m for m in loaded
                             if m == f"loa.{other}" or m.startswith(f"loa.{other}."))
            assert not reached, (
                f"loa.{folder} reaches into {other}: {reached} — a sense must be "
                f"one folder")


def test_no_daemon_or_driver_touches_the_cortex():
    """A daemon PUBLISHES. It never reads another service's state and never
    writes the store — that is the cross-service coupling this design removes.

    Not hypothetical: the 2026-09-13 deploy found motion, sonar and weather all
    still calling cortex.set_state()/log_event(). They had been rewritten in
    spirit and not in code, so the readings never reached the feed, and the new
    use_topic signature crashed all three on boot. The same read was still
    inside the drivers when they moved (the counts and the PIR's hold asked the
    cortex for numbers the driver had itself published), which is why the
    DRIVERS are checked here too.
    """
    # oled and ring are NOT in this list any more: those daemons are Rust and
    # have no Python file to parse. Their equivalent guarantee is structural —
    # a separate binary that speaks the topic cannot call into this interpreter
    # at all, which is a stronger version of the same rule.
    paths = [ROOT / name / "__main__.py"
             for name in ("weather", "fault")]
    paths += [ROOT / folder / f"{mod}.py" for folder, mod, _ in DRIVERS]
    for path in paths:
        src = path.read_text()
        # Parse rather than grep: a mention of the cortex in a comment or a
        # docstring is explanation, not a call. Only real attribute access on
        # the name `cortex` counts.
        bad = sorted({n.attr for n in ast.walk(ast.parse(src))
                      if isinstance(n, ast.Attribute)
                      and isinstance(n.value, ast.Name)
                      and n.value.id == "cortex"})
        assert not bad, f"{path.name} still calls cortex.{bad} — it must publish"


def test_every_sense_daemon_claims_its_topic():
    """use_topic() takes the topic name. Calling it bare is a TypeError at boot.

    The Rust senses do not do this at all: their topic is DERIVED from the
    payload on every send (`body_topic_of`), so there is no string to get wrong.
    This checks the Python daemons that are left.
    """
    for name, topic in (("weather", "weather"),):
        src = (ROOT / name / "__main__.py").read_text()
        assert f'use_topic("{topic}")' in src, f"{name} does not claim {topic}"
