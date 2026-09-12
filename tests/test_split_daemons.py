"""One process per sense.

Why the split exists, as a test rather than a comment: loa-sense ran the PIR,
the sonar, the DHT and the baro in one process, and the DHT read is a blocking
pulse-collection. Found live 2026-09-12 — `dht: read failed (10x) no pulses`
every ~100s, and the PIR silent for two hours while it was working fine.

So: the PIR must not share a thread with anything that can hang, and the split
must not quietly lose a setting on the way.
"""
import inspect
import re

from loa import motion, sonar, weather


def _cfg_keys(module):
    return set(re.findall(r'cfg\.get\("([a-z_]+)"', inspect.getsource(module)))


def test_each_sense_has_its_own_entrypoint():
    for mod in (motion, sonar, weather):
        assert callable(mod.main), f"{mod.__name__} has no main()"


def test_the_split_lost_no_setting():
    """Every `sense_*` key the combined daemon read must still be read by one
    of the three. A dropped key is a sensor that works in tests and not on the
    body — the failure mode is silence, so nothing would catch it."""
    import loa.sense as sense

    old = _cfg_keys(sense)
    new = _cfg_keys(motion) | _cfg_keys(sonar) | _cfg_keys(weather)
    lost = old - new
    assert not lost, f"the split dropped these settings: {sorted(lost)}"


def test_motion_cannot_be_starved_by_a_hung_sensor():
    """The whole point. If a blocking read ever moves back into the motion
    daemon, this fails before it can cost another two hours."""
    src = inspect.getsource(motion)
    for forbidden in ("DHT11", "BMP180", "Sonar("):
        assert forbidden not in src, (
            f"motion grew a {forbidden} — a hung sensor must not be able to "
            f"deafen the PIR")


def test_the_face_says_what_the_pin_says():
    """Motion is only useful if the PIR's own state is published for the face
    and the sweep. This is the thing that was true of the combined daemon and
    must stay true of the split one."""
    src = inspect.getsource(motion)
    assert "pir_high" in src, "motion must publish the pin level"
    assert "set_input" in src, "motion must own the pin's input mode"


def test_no_daemon_touches_the_cortex():
    """A daemon PUBLISHES. It never reads another service's state and never
    writes the store — that is the cross-service coupling this design removes.

    Not hypothetical: the 2026-09-13 deploy found motion, sonar and weather all
    still calling cortex.set_state()/log_event(). They had been rewritten in
    spirit and not in code, so the readings never reached the feed, and the new
    use_topic signature crashed all three on boot. The lesson is in the file
    above: a test written against the old assumption passes happily.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "loa"
    for name in ("motion", "sonar", "weather", "oled", "ring", "fault"):
        src = (root / f"{name}.py").read_text()
        # Parse rather than grep: a mention of the cortex in a comment or a
        # docstring is explanation, not a call. Only real attribute access on
        # the name `cortex` counts.
        bad = sorted({n.attr for n in ast.walk(ast.parse(src))
                      if isinstance(n, ast.Attribute)
                      and isinstance(n.value, ast.Name)
                      and n.value.id == "cortex"})
        assert not bad, f"{name}.py still calls cortex.{bad} — it must publish"


def test_every_sense_daemon_claims_its_topic():
    """use_topic() takes the topic name. Calling it bare is a TypeError at boot."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "loa"
    for name, topic in (("motion", "pir"), ("sonar", "sonar"),
                        ("weather", "weather")):
        src = (root / f"{name}.py").read_text()
        assert f'use_topic("{topic}")' in src, f"{name} does not claim {topic}"
