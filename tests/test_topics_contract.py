"""the wire contract — topics, one message each, nothing silently dropped.

The failure this file exists to prevent, from 2026-09-12: a ring frame was
hex-encoded, base64-decoded, and produced a right-SIZED, entirely wrong answer
that read as broken hardware. An ingest that dropped an unmapped field would do
the same thing a different way — the body would publish a plausible message that
never mentioned what the daemon measured.

So: every field of every topic message must be either mapped to a state key or
listed here as deliberately carried by hand. A new field with no home fails this
test instead of vanishing on the body.
"""
import time

import pytest

from loa import cortex, store, topic
from loa import cortexd
from loa.pb import loa_pb2 as pb

#: Fields carried by hand in cortexd._build / the ingest, not by the state map.
#: Each one is here because it is not a scalar state key: it is bytes, it is the
#: power map, it is a repeated row, or it is the timestamp of the message itself.
CARRIED_BY_HAND = {
    "ripperdoc": {"face", "ts"},
    "ring": {"ring", "ts"},
    "pir": {"ts"},
    "sonar": {"ts"},
    "baro": {"ts", "trend", "series"},
    "weather": {"ts"},
    "power": {"rails", "frag", "ts"},
    "fault": {"rows", "ts"},
    "event": {"ts", "kind", "detail"},
}


@pytest.fixture(autouse=True)
def clean_body():
    cortex.reset_for_tests()
    cortex.boot(None)
    yield
    cortex.reset_for_tests()


def test_every_topic_has_a_message():
    """A topic with no message in the schema is a topic nobody can decode."""
    for name in topic.TOPICS:
        assert name in pb.Envelope.DESCRIPTOR.fields_by_name, name


def test_no_field_is_silently_dropped():
    """The whole point of this file."""
    for name in topic.TOPICS:
        message = getattr(pb, _camel(name))
        mapped = set(cortexd.TOPIC_STATE_MAP.get(name, {}))
        known = mapped | CARRIED_BY_HAND[name]
        fields = {f.name for f in message.DESCRIPTOR.fields}
        unmapped = fields - known
        assert not unmapped, (
            "topic %r has fields nothing maps to the state: %s — either map "
            "them in TOPIC_STATE_MAP or carry them by hand in _build"
            % (name, sorted(unmapped)))


def _camel(name):
    return "".join(p.capitalize() for p in name.split("_"))


@pytest.mark.parametrize("name", cortexd.TICK_TOPICS)
def test_the_tick_builds_a_whole_message_per_topic(name):
    """Every topic the tick publishes builds, and builds as ITSELF."""
    msg = cortexd._build(name, cortex.get_state())
    assert msg.DESCRIPTOR.name == _camel(name)
    env = topic.envelope(name, msg)
    assert env.WhichOneof("body") == name
    assert env.schema_version == topic.SCHEMA_VERSION


def test_the_face_rides_inside_the_ripperdoc_message():
    """One protobuf event with the raw bytes inside it — not a side-channel."""
    cortexd._FACE = bytes(range(256)) * 4          # 1024 B
    msg = cortexd._build("ripperdoc", cortex.get_state())
    assert len(msg.face) == 1024
    assert msg.face[:4] == b"\x00\x01\x02\x03"


def test_ripperdoc_carries_how_the_body_is_presenting_itself():
    cortex.set_state({"page": "power", "mood": "pleased", "snr_on": False,
                      "ring_state": "off", "oled_dim": True})
    msg = cortexd._build("ripperdoc", cortex.get_state())
    assert msg.page == "power"
    assert msg.mood == "pleased"
    assert msg.snr_on is False
    assert msg.ring_state == "off"
    assert msg.oled_dim is True


def test_ripperdoc_carries_no_sensor_readings():
    """The screen already holds them. If it is drawn on the glass it arrives as
    glass; wanting the number is a different interest, so a different topic."""
    cortex.set_state({"snr_cm": 42.0, "temp_c": 21.5, "pressure_hpa": 1013.0})
    fields = {f.name for f in pb.Ripperdoc.DESCRIPTOR.fields}
    assert not (fields & {"snr_cm", "temp_c", "pressure_hpa", "pir_high"})


# -- the store blip is visible on the glass -------------------------------- #

def test_db_down_is_a_fault_row_and_a_hurts_condition():
    cortex.reset_for_tests()
    cortex.boot(None)                       # no store configured
    cortex._db_down = True
    rows = cortexd._fault_rows(cortex.get_state())
    assert any(r["code"] == "DB DOWN" for r in rows)
    assert cortexd._condition_from(rows) == "hurts"


def test_a_healthy_body_with_no_faults_reads_well():
    rows = cortexd._fault_rows(cortex.get_state())
    assert rows == []
    assert cortexd._condition_from(rows) == "well"


def test_a_warning_without_a_fault_is_a_niggle():
    rows = [{"level": "warn", "code": "RING STOPPED", "text": "stopped"}]
    assert cortexd._condition_from(rows) == "niggle"


# -- the wire itself ------------------------------------------------------- #

def test_a_message_rides_as_topic_then_envelope(ctx=None):
    """[topic name][Envelope], and the name is what a subscriber filters on."""
    import zmq
    ctx = zmq.Context()
    pub = topic.Publisher(endpoint="inproc://wiretest", ctx=ctx)
    sub = topic.Subscriber(endpoints=["inproc://wiretest"], ctx=ctx,
                           topics=["ripperdoc"])
    topic.wait_for_subscribers(0.2)
    pub.send("ring", pb.Ring(ring=b"x" * 72))
    pub.send("ripperdoc", pb.Ripperdoc(face=b"y" * 1024, page="power"))
    got = sub.recv(1000)
    assert got is not None, "the topic filter let the wrong topic through"
    assert got[0] == "ripperdoc"          # the ring was filtered OUT
    assert len(got[1].ripperdoc.face) == 1024
    assert sub.recv(200) is None          # and nothing else was delivered
    sub.close()
    pub.close()
    ctx.term()


def test_schema_version_mismatch_is_loud():
    env = pb.Envelope(schema_version=topic.SCHEMA_VERSION + 1)
    env.ring.ring = b"z"
    with pytest.raises(topic.SchemaMismatch):
        topic.check(env)


def test_topic_and_payload_disagreeing_is_loud():
    """A ripperdoc frame carrying a baro message must not be guessed at."""
    env = topic.envelope("baro", pb.Baro(pressure_hpa=1013.0))
    with pytest.raises(topic.TopicMismatch):
        topic.check_topic("ripperdoc", env)


def test_unknown_topic_is_refused():
    with pytest.raises(ValueError):
        topic.envelope("frames")


def test_receiver_rejects_an_old_schema_without_stopping(ctx=None):
    """An old daemon left running must not be able to stop the body reporting
    itself — counted, dropped, and visible."""
    import zmq
    ctx = zmq.Context()
    rx = topic.Receiver(endpoint="inproc://rx-test", ctx=ctx)
    tx = topic.Sender(endpoint="inproc://rx-test", ctx=ctx)
    stale = pb.Envelope(schema_version=4)
    stale.ring.ring = b"old"
    tx.sock.send_multipart([b"ring", stale.SerializeToString()])
    topic.wait_for_subscribers(0.2)
    assert rx.recv(500) is None
    assert rx.rejected == 1
    rx.close()
    tx.close()
    ctx.term()


def test_events_stay_scalars():
    """Events keep to scalars: a bytes field here would be silently base64'd by
    the canonical JSON mapping and smuggle an encoding into the database."""
    ev = pb.Event(ts=1.0, kind="frame")
    ev.detail["face_sha"] = "abc123"
    assert set(pb.Event.DESCRIPTOR.fields_by_name) == {"ts", "kind", "detail"}
