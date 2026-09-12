"""A daemon's reading goes on the topic and reaches a subscriber.

The bug this exists to prevent: the daemons are separate processes and once
wrote the sqlite file directly, so the cortex's publisher never saw their
changes and the feed carried NOTHING from the senses. Divv was rightly furious
— "why is data arriving via /state and not the pub/sub I insisted on" — and the
answer was that the feed was empty and I had called it built.

Also the merge rule: a reading carries only the fields its daemon measured. A
motion daemon publishing pir_high=false must not clobber the mood.
"""
import sqlite3
import time

from loa import cortex, cortexd, topic


def _wait_for(predicate, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_a_reading_reaches_a_subscriber_without_clobbering_the_state(
        fresh_db, clean_publishers):
    inbox, outbox = "inproc://t-in", "inproc://t-out"
    cortex.set_state({"mood": "busy", "oled_mode": "scope"})

    cortexd.start_publishing(endpoint=outbox)
    _rx, stop = cortexd.start_ingesting(endpoint=inbox)
    try:
        sub = topic.Subscriber(endpoints=[outbox])
        tx = topic.Sender(endpoint=inbox)
        topic.wait_for_subscribers(None, 0.2)

        tx.send_state({"pir_high": False, "sense_count": 7})

        env = sub.recv(3000)
        assert env is not None, "a daemon's reading never reached the feed"
        assert env.WhichOneof("body") == "state"
        assert env.state.sense_count == 7, "the reading did not arrive intact"
        assert env.state.mood == "busy", (
            "a motion reading clobbered the mood — the merge is not by presence")
        assert env.state.oled_mode == "scope", "a partial reading wiped oled_mode"

        assert cortex.get_state()["mood"] == "busy"
        assert not cortex.get_state()["pir_high"]
        sub.close(); tx.close()
    finally:
        stop()


def test_a_pir_going_true_then_false_is_both_published(fresh_db, clean_publishers):
    """false is a reading, not a silence. Without explicit presence the false
    would be indistinguishable from 'not mentioned' and the body would stay lit
    after the room went quiet."""
    inbox, outbox = "inproc://t2-in", "inproc://t2-out"
    cortexd.start_publishing(endpoint=outbox)
    _rx, stop = cortexd.start_ingesting(endpoint=inbox)
    try:
        sub = topic.Subscriber(endpoints=[outbox])
        tx = topic.Sender(endpoint=inbox)
        topic.wait_for_subscribers(None, 0.2)

        tx.send_state({"pir_high": True})
        env = sub.recv(3000)
        assert env is not None and env.state.pir_high is True

        tx.send_state({"pir_high": False})
        env = sub.recv(3000)
        assert env is not None, "the PIR going quiet published nothing"
        assert env.state.pir_high is False
        sub.close(); tx.close()
    finally:
        stop()


def test_a_daemon_event_becomes_a_record(fresh_db, clean_publishers):
    """Events go both ways: subscribers see them live, the recorder writes
    them. The cortex is neither the only reader nor the only writer."""
    inbox = "inproc://t3-in"
    _rx, stop = cortexd.start_ingesting(endpoint=inbox)
    try:
        tx = topic.Sender(endpoint=inbox)
        tx.send_event(1789200000.0, "sense", {"kind": "pir", "count": "3"})

        def recorded():
            try:
                return sqlite3.connect(str(fresh_db)).execute(
                    "select count(*) from events").fetchone()[0] == 1
            except sqlite3.OperationalError:
                return False

        assert _wait_for(recorded), "a published event was never recorded"
        row = sqlite3.connect(str(fresh_db)).execute(
            "select ts, kind, detail from events").fetchone()
        assert row[0] == 1789200000.0, "recorded when it happened, not when it landed"
        assert "pir" in row[2]
        tx.close()
    finally:
        stop()
