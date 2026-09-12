"""The cortex is the ONE publisher.

Divv's shape: one source, many consumers. loa-cortex publishes; everything that
needs data subscribes. It does not keep private copies for the feed, RAM and the
DB — three outputs is three things that can drift.

The hook lives on the state module, which knows nothing about ZMQ: it reports
what changed, the service decides what to do with it.
"""
import pytest

from loa import cortex, cortexd, topic


def test_a_state_change_becomes_a_message(fresh_db, clean_publishers):
    ep = "inproc://t-pub-state"
    cortexd.start_publishing(endpoint=ep)
    sub = topic.Subscriber(endpoints=[ep])
    topic.wait_for_subscribers(None, 0.15)

    cortex.set_state({"mood": "busy"})

    env = sub.recv(2000)
    assert env is not None, "a state change produced no message"
    assert env.WhichOneof("body") == "state"
    assert env.state.mood == "busy"
    sub.close()


def test_an_unchanged_state_is_not_republished(fresh_db, clean_publishers):
    """The schema decides what 'changed' means. A state that has not moved is
    not news, and republishing it is how a feed becomes noise nobody reads."""
    ep = "inproc://t-pub-dedupe"
    cortexd.start_publishing(endpoint=ep)
    sub = topic.Subscriber(endpoints=[ep])
    topic.wait_for_subscribers(None, 0.15)

    cortex.set_state({"mood": "busy"})
    assert sub.recv(2000) is not None, "the first change must publish"

    cortex.set_state({"mood": "busy"})              # same value, again
    assert sub.recv(400) is None, "an unchanged state was republished"
    sub.close()


def test_an_event_becomes_a_message_and_a_record(fresh_db, clean_publishers):
    """Events go both ways by design: subscribers see them live, and the
    recorder consumer writes them. The publisher does not write the database —
    it is not an output of the cortex."""
    import sqlite3
    ep = "inproc://t-pub-event"
    cortexd.start_publishing(endpoint=ep)
    sub = topic.Subscriber(endpoints=[ep])
    topic.wait_for_subscribers(None, 0.15)

    cortex.log_event("boot", {"svc": "sonar"}, ts=1789200000.0)

    env = sub.recv(2000)
    assert env is not None and env.WhichOneof("body") == "event"
    assert env.event.kind == "boot"
    assert dict(env.event.detail)["svc"] == "sonar"
    assert env.event.ts == 1789200000.0

    row = sqlite3.connect(str(fresh_db)).execute(
        "select ts from events").fetchone()
    assert row[0] == 1789200000.0
    sub.close()


def test_a_broken_listener_cannot_break_a_state_write(fresh_db, clean_publishers):
    """A subscriber must never be able to stop the body recording its own
    state. The publisher is a consumer of the state module, not a gate on it."""
    def explode(kind, payload):
        raise RuntimeError("subscriber is unhappy")

    off = cortex.on_publish(explode)
    try:
        cortex.set_state({"mood": "calm"})      # must not raise
        assert cortex.get_state()["mood"] == "calm"
    finally:
        off()
