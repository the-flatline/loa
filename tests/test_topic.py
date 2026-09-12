"""topic — the socket layer, and the canonical JSON a record is stored as.

Replaced wholesale on 2026-09-12: the file it replaces tested the old wire (one
stream, one envelope, state and frames as separate messages). Every assertion in
it named a shape that no longer exists, and it hung the suite waiting on sockets
that never opened — a test that cannot fail is worse than no test.
"""
import time

import pytest
import zmq

from loa import topic
from loa.pb import loa_pb2 as pb


@pytest.fixture
def ctx():
    c = zmq.Context()
    yield c
    # destroy(), not term(): a failed assert leaves sockets open, and term()
    # then blocks forever — which turns a red test into a hung suite. A test
    # that hangs is worse than a test that fails, because it hides everything
    # behind it.
    c.destroy(linger=0)


def test_the_topic_name_is_the_filter(ctx):
    """A subscriber to `pir` is not sent `sonar`. This is the whole point of
    naming messages by subject instead of by shape."""
    pub = topic.Publisher(endpoint="inproc://filter", ctx=ctx)
    sub = topic.Subscriber(endpoints=["inproc://filter"], topics=["pir"], ctx=ctx)
    topic.wait_for_subscribers(0.3)
    pub.send("sonar", pb.Sonar(cm=41.0))
    pub.send("pir", pb.Pir(high=True))
    got = sub.recv(1000)
    assert got is not None
    assert got[0] == "pir"
    assert got[1].pir.high is True
    sub.close()
    pub.close()


def test_a_reading_survives_the_wire(ctx):
    pub = topic.Publisher(endpoint="inproc://round", ctx=ctx)
    sub = topic.Subscriber(endpoints=["inproc://round"], topics=["baro"], ctx=ctx)
    topic.wait_for_subscribers(0.3)
    pub.send("baro", pb.Baro(pressure_hpa=1013.2, temp_c=21.5))
    name, env = sub.recv(1000)
    assert name == "baro"
    assert env.baro.pressure_hpa == pytest.approx(1013.2)
    assert env.baro.temp_c == pytest.approx(21.5)
    assert env.schema_version == topic.SCHEMA_VERSION
    sub.close()
    pub.close()


def test_only_the_fields_that_were_set_are_present(ctx):
    """Presence, not zeroes: a reading that never mentioned a value must not
    clobber the value another daemon owns with a default."""
    msg = topic.partial("pir", {"pir_high": True})
    assert msg.HasField("high")
    assert not msg.HasField("count")
    assert not msg.HasField("on_ts")


def test_a_reading_under_a_name_nothing_reads_is_an_error():
    """The silent-drop guard: a field published under a key the ingest does not
    read looks exactly like a sensor that never fired."""
    with pytest.raises(KeyError):
        topic.partial("pir", {"pir_high": 1, "snr_cm": 41.0})


def test_the_daemon_leg_round_trips_with_its_topic(ctx):
    rx = topic.Receiver(endpoint="inproc://leg", ctx=ctx)
    tx = topic.Sender(endpoint="inproc://leg", ctx=ctx)
    topic.wait_for_subscribers(0.3)
    tx.send("weather", topic.partial("weather", {"temp_c": 19.0, "hum_pct": 55.0}))
    name, env = rx.recv(1000)
    assert name == "weather"
    assert topic.dict_to_state("weather", env.weather)["temp_c"] == 19.0
    tx.close()
    rx.close()


def test_an_event_is_always_an_event():
    """Whatever raised it. The kind carries the source."""
    msg = topic.event(1726000000.0, "sense", {"svc": "motion", "gpio": 17})
    assert msg.kind == "sense"
    assert msg.detail["svc"] == "motion"
    env = topic.envelope("event", msg)
    assert env.WhichOneof("body") == "event"


def test_an_event_detail_stays_scalar():
    """A bytes field here would be silently base64'd by the canonical JSON
    mapping and smuggle an encoding into the database."""
    msg = topic.event(1.0, "x", {"n": 5, "ok": True})
    assert msg.detail["n"] == "5" and msg.detail["ok"] == "True"


# -- the stored form ------------------------------------------------------- #

def test_canonical_json_uses_the_schema_names():
    msg = pb.Sonar(cm=41.0, count=3)
    out = topic.message_to_json(msg)
    assert '"cm"' in out and '"count"' in out


def test_canonical_json_carries_a_reading_whole():
    """A stored record must not lose a field to a hand-written mapping: the
    schema generates both the wire form and the stored form, so they cannot
    disagree about what a field is called."""
    msg = topic.partial("pir", {"pir_high": True, "pir_last_hold": 4.0})
    out = topic.message_to_json(msg)
    assert "high" in out and "last_hold" in out


def test_check_refuses_a_stale_schema():
    env = pb.Envelope(schema_version=topic.SCHEMA_VERSION - 1)
    env.ring.ring = b"x"
    with pytest.raises(topic.SchemaMismatch):
        topic.check(env)


def test_check_topic_refuses_a_payload_that_is_not_the_topic(ctx):
    env = topic.envelope("ring", pb.Ring(ring=b"x" * 72))
    with pytest.raises(topic.TopicMismatch):
        topic.check_topic("ripperdoc", env)


def test_an_unknown_topic_is_refused_at_build():
    with pytest.raises(ValueError):
        topic.envelope("frames")


def test_the_mirror_keeps_the_newest_message_per_topic(ctx):
    pub = topic.Publisher(endpoint="inproc://mirror", ctx=ctx)
    m = topic.Mirror(endpoints=["inproc://mirror"], topics=["ripperdoc", "fault"],
                     ctx=ctx)
    topic.wait_for_subscribers(0.3)
    pub.send("ripperdoc", pb.Ripperdoc(page="power", mood="pleased"))
    pub.send("fault", pb.Fault(condition="hurts"))
    deadline = time.time() + 3
    while time.time() < deadline and m.message("fault") is None:
        time.sleep(0.05)
    assert m.message("ripperdoc").page == "power"
    assert m.message("fault").condition == "hurts"
    assert m.state()["mood"] == "pleased"
    assert m.state()["condition"] == "hurts"
    assert m.age("ripperdoc") is not None
    assert m.age("baro") is None          # never arrived, and says so
    m.close()
    pub.close()
