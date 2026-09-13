"""The init handshake — a restarted brain re-learns the body.

Divv's design, verbatim: "cortex should send some sort of init that tells the
daemons to send a full payload right away, and then it will carry on with
changes only."

ZMQ is DIRECTIONAL here: the daemons PUSH up and the cortex PULLs, so the cortex
cannot answer a daemon and cannot ask one anything down that leg. The ask
therefore travels the OTHER way — out on a topic the daemons SUBSCRIBE to.
(Same thing as a reply, arriving by the only road that leads that way.)

Both halves are covered: a daemon answers an init with its whole current
payload, and a daemon also sends its full payload on its own start.
"""
import zmq

from loa import cortexd, sense, topic as t


def test_the_cortex_publishes_an_init_a_daemon_can_hear():
    """The ask goes OUT on a subscribed topic, and it is the init message."""
    ctx = zmq.Context()
    pub = t.Publisher(endpoint="inproc://init-test", ctx=ctx)
    sub = t.Subscriber(endpoints=["inproc://init-test"], ctx=ctx, topics=["init"])
    t.wait_for_subscribers(0.2)
    try:
        cortexd.publish_init(pub, asks=1, gap=0.0)
        got = sub.recv(1000)
        assert got is not None, "the init never reached the subscriber"
        name, env = got
        assert name == "init"
        assert env.WhichOneof("body") == "init"
        assert env.init.source == "cortex"
    finally:
        sub.close()
        pub.close()
        ctx.term()


def test_the_init_is_published_more_than_once():
    """PUB/SUB has no backpressure: a daemon whose SUB is still reconnecting
    drops the first ask silently. Several asks cover the reconnect, and then it
    STOPS — a repeating init would keep every daemon re-sending for nothing."""
    class Counting:
        def __init__(self):
            self.sent = []
        def send(self, name, msg):
            self.sent.append(name)

    pub = Counting()
    cortexd.publish_init(pub, asks=3, gap=0.0)
    assert pub.sent == ["init", "init", "init"]


def test_a_daemon_answers_an_init_with_its_FULL_payload(monkeypatch):
    """Not a change: everything it has. A change-only sensor has nothing to
    change against on a cortex that just came up."""
    sent = []

    class Sock:
        def send(self, topic, msg):
            sent.append((topic, msg))

    monkeypatch.setattr(sense, "sender_for", lambda topic, endpoint=None: Sock())
    monkeypatch.setattr(sense, "_TOPIC", "pir")
    monkeypatch.setattr(sense, "_LAST", {}, raising=False)

    sense.publish({"pir_high": 1, "pir_on_ts": 5.0})
    sense.publish({"pir_count": 7})
    sent.clear()

    sense.republish_full()

    assert [name for name, _ in sent] == ["pir"]
    _name, msg = sent[0]
    assert msg.HasField("high") and msg.high is True
    assert msg.HasField("count") and msg.count == 7
    assert msg.HasField("on_ts") and msg.on_ts == 5.0


def test_a_daemon_that_hosts_two_sources_answers_for_both(monkeypatch):
    """loa-weather carries the weather board AND the baro; the answer must
    arrive under each name, exactly like the readings do."""
    sent = []

    class Sock:
        def send(self, topic, msg):
            sent.append(topic)

    monkeypatch.setattr(sense, "sender_for", lambda topic, endpoint=None: Sock())
    monkeypatch.setattr(sense, "_TOPIC", "weather")
    monkeypatch.setattr(sense, "_LAST", {}, raising=False)

    sense.publish({"temp_c": 21.5}, topic="weather")
    sense.publish({"pressure_hpa": 1013.0}, topic="baro")
    sent.clear()

    sense.republish_full()
    assert sorted(sent) == ["baro", "weather"]


def test_the_init_is_a_topic_and_not_a_state():
    """It is a control message: it must be in TOPICS (a daemon subscribes to it)
    and out of the tick (it is not a reading)."""
    assert "init" in t.TOPICS
    assert "init" not in cortexd.TICK_TOPICS
