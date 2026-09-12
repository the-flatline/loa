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
