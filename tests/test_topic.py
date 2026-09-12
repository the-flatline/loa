"""The topic: one schema, loud failures, raw bytes.

The failures this pins down are the ones that cost tonight: a wrong answer that
looked right. A schema version that disagrees must RAISE, not decode. The ring
frame must arrive as raw bytes, not as hex, not as base64 — so nothing can
guess at the encoding and be plausible about it.
"""
import zmq

from loa import topic
from loa.pb import loa_pb2 as pb

STATE = {
    "mood": "calm",
    "ring_state": "home",
    "oled_mode": "scope",
    "oled_text": "LOA",
    "oled_dim": False,
    "ripperdoc": True,
    "ripperdoc_page": "sensors",
    "condition": "well",
    "pir_high": False,
    "pir_count": 3,
    "pir_last_hold": 4.25,
    "snr_cm": 41.5,
    "temp_c": 23.8,
    "pressure_hpa": 1026.26,
}


def _sub(endpoint, ctx):
    """A subscriber that is actually subscribed before anyone publishes.

    PUB/SUB drops to a slow joiner — silently — which would make these tests
    flaky in exactly the way that teaches you nothing."""
    s = topic.Subscriber(endpoints=[endpoint], ctx=ctx)
    topic.wait_for_subscribers(None, 0.15)
    return s


def test_state_round_trips_through_the_schema_mapping():
    msg = topic.state_to_message(STATE)
    back = topic.message_to_state(msg)
    for k, v in STATE.items():
        assert back[k] == v, f"{k} changed: {v!r} -> {back[k]!r}"


def test_unknown_keys_do_not_break_the_publish_path():
    """Internal extras (counters, caches) must not stop the body reporting."""
    msg = topic.state_to_message({**STATE, "some_internal_cache": {"a": 1}})
    assert msg.mood == "calm"


def test_canonical_json_keeps_snake_case():
    """Default canonical JSON turns ring_state into ringState — camelCase in the
    database, snake_case in the code. That is drift with a new haircut."""
    js = topic.message_to_json(topic.state_to_message(STATE))
    assert "ring_state" in js
    assert "ringState" not in js


def test_a_schema_mismatch_raises_instead_of_guessing():
    env = topic._envelope(schema_version=topic.SCHEMA_VERSION + 1)
    try:
        topic.check(env)
    except topic.SchemaMismatch as e:
        assert "refusing to guess" in str(e)
    else:
        raise AssertionError("a mismatched schema decoded instead of failing")


def test_bytes_cross_the_wire_raw():
    """No hex, no base64. If this ever fails, look for an encoding somebody
    added 'to make it json-friendly'."""
    ctx = zmq.Context()
    ep = "inproc://t-twin"
    pub = topic.Publisher(ep, ctx=ctx)
    sub = _sub(ep, ctx)

    face = bytes(range(256)) * 4            # 1024, exactly a panel
    ring = bytes([0, 0, 216] * 24)          # 72, 24 px red
    pub.publish_frames(face, ring)

    env = sub.recv(2000)
    assert env is not None, "no frames arrived"
    assert env.frames.face == face, "the face did not arrive byte-for-byte"
    assert env.frames.ring == ring, "the ring did not arrive byte-for-byte"
    assert len(env.frames.ring) == 72
    pub.close(); sub.close()


def test_state_publishes_and_is_typed_on_arrival():
    ctx = zmq.Context()
    ep = "inproc://t-state"
    pub = topic.Publisher(ep, ctx=ctx)
    sub = _sub(ep, ctx)

    pub.publish_state(STATE)
    env = sub.recv(2000)
    assert env is not None
    assert env.WhichOneof("body") == "state"
    assert env.state.mood == "calm" and env.state.pir_count == 3
    pub.close(); sub.close()


def test_a_daemon_feeds_the_cortex_and_a_stale_one_cannot_silence_it():
    """The inbound leg is PUSH/PULL: many daemons, one collector, and a reading
    that vanishes because a consumer was slow would be a lie about the body."""
    ctx = zmq.Context()
    ep = "inproc://t-in"
    rx = topic.Receiver(ep, ctx=ctx)
    tx = topic.Sender(ep, ctx=ctx)

    tx.send_state({"pir_high": True, "pir_count": 1})
    env = rx.recv(2000)
    assert env is not None and env.state.pir_high is True

    # a daemon left running from an older deploy: counted and dropped, never
    # raised into the cortex's loop — it must not stop the body reporting
    stale = topic._envelope(schema_version=topic.SCHEMA_VERSION + 1)
    stale.state.mood = "wrong"
    tx.send(stale)
    assert rx.recv(1500) is None
    assert rx.rejected == 1, "a stale daemon must be visible, not silent"

    tx.close(); rx.close()


def test_events_carry_scalars_and_land_as_json():
    """detail is a string map, so no bytes field can be silently base64'd into
    the database where nobody looks for an encoding."""
    env = topic._envelope()
    env.event.ts = 1789200000.0
    env.event.kind = "boot"
    env.event.detail["svc"] = "motion"
    js = topic.message_to_json(env.event)
    assert '"svc": "motion"' in js or '"svc":"motion"' in js
    assert "base64" not in js.lower()
