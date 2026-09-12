"""topic — the one source. Publish once, subscribe everywhere.

The shape, settled with Divv 2026-09-12:

    senses + fault  --push-->  loa-cortex  --PUB-->  THE TOPICS
                                                       (protobuf, ZeroMQ)
                                                          |
         subscribers, each independent, each one job: <---+
           loa-oled, loa-ring   (subscribe; never read the state)
           ripperdoc            (the console, on dixie)
           the web app          (its own server subscribes)
           records              (written by the cortex, the only DB writer)

TOPICS, not shapes. A topic is something a consumer can choose to be
interested in. `state` and `frames` are shapes, not subjects — nobody is
interested in "bytes that happen to be pixels", and splitting them into two
messages broke the one-message contract (keep the last message, render it).

    ripperdoc            the screen + the tiny bits that are not in the pixels
    ring                 the ring's pixels
    pir sonar baro weather
    fault                what hurts
    event                something happened — a record

THE TICK. The cortex republishes EVERY topic on a fixed tick (TICK_S) and again
immediately when something changes. Two triggers, one path.

The tick is not decoration. Publish-on-change-only means a consumer that
subscribes after the last change is blind, and someone will "fix" that with a
/subscribe endpoint returning initial state — which is the endpoint a future
session will start hammering. The tick removes the need for it. A consumer is
therefore at most TICK_S behind, always, from the moment it subscribes, and
there is nothing to hand it on the way in.

The cost is republishing an unchanged frame a few times a second: a couple of kB
on a LAN. That is cheaper than a bootstrap path, and it is the whole reason the
consumer contract is four words:

    subscribe -> keep the last message -> render -> repeat

WIRE FORM. Two ZMQ frames: [topic name][Envelope]. The name is what ZMQ matches
a subscription against, so a consumer that wants only the ring never has pixels
sent to it — the topic IS the filter, natively, with no layer built on top. The
Envelope's oneof is the type check, and a topic and payload that disagree raise
rather than decode.

Four socket roles, because the direction of data differs:
  Publisher  (PUB)  — the cortex, and only the cortex
  Subscriber (SUB)  — every consumer
  Sender     (PUSH) — the daemons feeding the cortex
  Receiver   (PULL) — the cortex collecting from the daemons

PUSH/PULL rather than PUB/SUB for the inbound leg: many daemons, one collector,
and a reading that vanishes because a subscriber was slow is a lie about the
body. PUSH/PULL queues; PUB/SUB drops.
"""
import json
import threading
import time

import zmq
from google.protobuf import json_format

from .pb import loa_pb2 as pb

#: Bump on ANY change to proto/loa.proto. Publisher and subscriber check it, so
#: a body and a console that disagree fail on arrival instead of misreading.
#: v5: topics. Each message rides as [topic name][Envelope]; one message per
#: topic, the face bytes INSIDE the ripperdoc message, and a `fault` topic so a
#: consumer never has to split a "level|label" string to find out what hurts.
SCHEMA_VERSION = 6

#: Every topic the cortex publishes. The oneof field names in proto/loa.proto
#: match these exactly — that is what lets topic_payload() stay generic.
TOPICS = ("ripperdoc", "ring", "pir", "sonar", "baro", "weather", "power",
          "fault", "event")

#: 2Hz. Every topic goes out this often, changed or not.
TICK_S = 0.5

#: Where consumers subscribe (the cortex binds this; it must be reachable from
#: dixie, so it is the LAN interface, not loopback).
DEFAULT_ENDPOINT = "tcp://0.0.0.0:5556"

#: Where daemons feed the cortex (local to the body, so loopback).
DEFAULT_INBOUND = "tcp://127.0.0.1:5555"

#: Long enough for a subscriber to establish a subscription before the first
#: publish, short enough not to be a stall. PUB/SUB drops to a slow joiner.
JOIN_GRACE_S = 0.25


class SchemaMismatch(RuntimeError):
    """Publisher and subscriber disagree about the wire format.

    Raised loudly and on purpose. The alternative is decoding a message whose
    fields mean something else, which produces plausible rubbish and gets
    blamed on the hardware.
    """


class TopicMismatch(RuntimeError):
    """The topic frame and the payload inside it disagree.

    Also raised loudly: a ripperdoc frame carrying a baro message means someone
    assembled a message wrong, and the consumer must not guess which half it
    believes.
    """


# --------------------------------------------------------------------------- #
# envelope <-> topic
# --------------------------------------------------------------------------- #

def envelope(topic, msg=None, schema_version=SCHEMA_VERSION) -> pb.Envelope:
    """An Envelope with `msg` in the oneof field named by `topic`.

    Generic on purpose: the topic name IS the oneof name, so adding a topic to
    the schema cannot leave a hand-written branch behind.
    """
    if topic not in TOPICS:
        raise ValueError("unknown topic %r (have %s)" % (topic, ", ".join(TOPICS)))
    env = pb.Envelope(schema_version=schema_version)
    if msg is not None:
        getattr(env, topic).CopyFrom(msg)
    return env


def check_topic(topic, env) -> None:
    """Raise unless this envelope's payload is the one `topic` promises."""
    body = env.WhichOneof("body")
    if body != topic:
        raise TopicMismatch(
            "topic frame says %r, payload is %r — refusing to guess which half "
            "is right" % (topic, body))


def check(envelope) -> None:
    """Raise SchemaMismatch unless this message is our schema."""
    if envelope.schema_version != SCHEMA_VERSION:
        raise SchemaMismatch(
            "wire schema v%d, this build speaks v%d — refusing to guess at the "
            "fields" % (envelope.schema_version, SCHEMA_VERSION))


# --------------------------------------------------------------------------- #
# encoding — always via the schema's own mapping, never a hand-written encoder
# --------------------------------------------------------------------------- #

def message_to_json(msg, indent=None) -> str:
    """Canonical JSON for a message.

    Two settings are NOT optional:

    preserving_proto_field_name — the default turns ring_state into ringState,
    which would put camelCase in the database and snake_case in the code. Drift
    with a new haircut.

    always_print_fields_with_no_presence — without it, canonical JSON OMITS any
    field sitting at its default, so oled_dim=False simply vanishes. A consumer
    then cannot tell "false now" from "not mentioned", and keeps the stale True
    it already had. A snapshot must state every field."""
    return json_format.MessageToJson(
        msg, preserving_proto_field_name=True, indent=indent,
        always_print_fields_with_no_presence=True)


def message_to_dict(msg) -> dict:
    """A message as a plain dict, through the same canonical mapping.

    Uses dataclass-style field names, not the default camelCase, so a stored
    record and the code that reads it agree about what a field is called.
    """
    return json.loads(message_to_json(msg))


def fields_present(msg) -> dict:
    """Only the fields this message actually carries.

    Every scalar field is `optional`, so presence is the difference between
    "I measured false" and "I am not talking about that". Merging a partial
    reading without this test is how a motion daemon clobbers the mood.

    Maps and repeated fields have no presence — empty means not carried.
    """
    out = {}
    for field in msg.DESCRIPTOR.fields:
        value = getattr(msg, field.name)
        is_map = (field.message_type is not None
                  and field.message_type.GetOptions().map_entry)
        if is_map:
            if len(value):
                out[field.name] = dict(value)
            continue
        # protobuf 7 dropped FieldDescriptor.label; is_repeated replaced it.
        # Getting this wrong threw on EVERY message — and the ingest caught it
        # and slept, so the feed was silently empty. Never swallow silently.
        try:
            repeated = field.is_repeated
        except AttributeError:                                  # pragma: no cover
            repeated = field.label == field.LABEL_REPEATED
        if repeated:
            if len(value):
                out[field.name] = list(value)
            continue
        if msg.HasField(field.name):
            out[field.name] = value
    return out


#: topic -> {proto field: state key}. ONE table, used in BOTH directions: the
#: ingest reads it to merge an inbound reading, and a daemon writes it (via
#: partial()) to publish one. A field with no entry is a reading that would go
#: out under a name nothing reads, so a test asserts every field of every topic
#: message is either mapped here or explicitly carried by hand.
TOPIC_STATE_MAP = {
    "ripperdoc": {"page": "page", "mood": "mood", "snr_on": "snr_on",
                  "ring_state": "ring_state", "oled_mode": "oled_mode",
                  "oled_text": "oled_text", "oled_dim": "oled_dim",
                  "ripperdoc": "ripperdoc", "condition": "condition"},
    "pir": {"high": "pir_high", "count": "pir_count", "last_ts": "pir_last_ts",
            "last_hold": "pir_last_hold", "on_ts": "pir_on_ts",
            "ts": "pir_last_ts"},
    "sonar": {"cm": "snr_cm", "count": "snr_count", "ts": "snr_ts"},
    "baro": {"pressure_hpa": "pressure_hpa", "temp_c": "baro_temp_c",
             "ts": "baro_ts", "count": "baro_count"},
    "weather": {"temp_c": "temp_c", "hum_pct": "hum_pct", "ts": "temp_ts",
                "count": "temp_count"},
    "fault": {"condition": "condition"},
    "ring": {},
}


def message_class(topic):
    """The protobuf class for a topic name. The topic name IS the message name,
    so a new topic cannot leave a hand-written branch behind."""
    return getattr(pb, "".join(p.capitalize() for p in topic.split("_")))


def partial(topic, fields):
    """A message for `topic` carrying only what `fields` names.

    The daemons speak state keys — pir_high, snr_cm — and this is the one place
    that knows how a key is spelled on the wire. A key with no wire field is an
    error rather than a silent drop: a reading that goes out under a name nothing
    reads looks exactly like a sensor that never fired.
    """
    msg = message_class(topic)()
    mapping = TOPIC_STATE_MAP.get(topic, {})
    known = set(mapping.values())
    unknown = {k for k in fields if k not in known}
    if unknown:
        raise KeyError("topic %r has no field for %s"
                       % (topic, ", ".join(sorted(unknown))))
    for field, key in mapping.items():
        value = fields.get(key)
        if value is not None:
            setattr(msg, field, value)
    return msg


def event(ts, kind, detail=None):
    """An Event message, always for the `event` topic.

    An event carries its own `kind`, so it does not need a topic of its own to
    say what it is — the money is in the message, not in the subject. Every
    daemon's records go here, and the source travels in `kind`/`detail` the way
    it already did.
    """
    return pb.Event(ts=float(ts), kind=str(kind),
                    detail={str(k): str(v) for k, v in (detail or {}).items()})


def dict_to_state(topic, msg):
    """The inbound direction: only the fields the message actually carries."""
    mapping = TOPIC_STATE_MAP.get(topic, {})
    out = {}
    for field, key in mapping.items():
        if msg.HasField(field):
            out[key] = getattr(msg, field)
    return out


class Mirror:
    """The newest message of each topic, kept in RAM for a local consumer.

    A renderer needs the body's current mode and mood. It must not read the
    cortex to get them — a daemon reaching into another service's state is the
    cross-service read this whole design removes, and it is what made a face
    daemon report a mood that the body had already dropped.

    So it subscribes, like anything else, and keeps the last message per topic.
    `state()` hands back the merged fields in the same flat dict the renderers
    already speak, so nothing downstream has to learn a new shape.
    """

    def __init__(self, topics=None, endpoints=None, ctx=None):
        self._sub = Subscriber(endpoints=endpoints, topics=topics, ctx=ctx)
        self._latest = {}
        self._at = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            got = self._sub.recv(200)
            if got is None:
                continue
            name, env = got
            with self._lock:
                self._latest[name] = getattr(env, name)
                self._at[name] = time.monotonic()

    def message(self, topic):
        """The newest message for a topic, or None if none has arrived."""
        with self._lock:
            return self._latest.get(topic)

    def age(self, topic):
        """Seconds since the last message on a topic, or None."""
        with self._lock:
            at = self._at.get(topic)
        return None if at is None else time.monotonic() - at

    def state(self):
        """Every topic's newest fields, merged into one flat state dict."""
        with self._lock:
            latest = dict(self._latest)
        out = {}
        for name, msg in latest.items():
            out.update(dict_to_state(name, msg))
        return out

    def close(self):
        """Stop the reader and WAIT for it. A thread still holding a socket makes
        libzmq's context terminate block forever, which turns a leaked consumer
        into a hung process — a fault that looks like the feed, not like a bug."""
        self._stop.set()
        self._sub.close()
        self._thread.join(timeout=1.0)


# --------------------------------------------------------------------------- #
# sockets
# --------------------------------------------------------------------------- #

def _context(ctx=None):
    return ctx or zmq.Context.instance()


class Publisher:
    """The cortex's PUB socket. There should be exactly one in the system."""

    def __init__(self, endpoint=DEFAULT_ENDPOINT, ctx=None, bind=True):
        self.sock = _context(ctx).socket(zmq.PUB)
        self.sock.setsockopt(zmq.LINGER, 0)
        (self.sock.bind if bind else self.sock.connect)(endpoint)
        self.endpoint = endpoint
        self.sent = 0

    def send(self, topic: str, msg=None, env=None) -> pb.Envelope:
        """[topic name][Envelope]. The name is the subscription filter."""
        if env is None:
            env = envelope(topic, msg)
        self.sock.send_multipart([topic.encode(), env.SerializeToString()])
        self.sent += 1
        return env

    def publish_event(self, ts: float, kind: str, detail=None):
        ev = pb.Event(ts=float(ts), kind=kind)
        for k, v in (detail or {}).items():
            # string map: events stay scalars, so no bytes field can be
            # silently base64'd into the database
            ev.detail[str(k)] = str(v)
        return self.send("event", ev)

    def close(self):
        self.sock.close()


class Subscriber:
    """Any consumer. Subscribes to the topics it is interested in and reads."""

    def __init__(self, endpoints=None, ctx=None, topics=TOPICS):
        ep = endpoints or [DEFAULT_ENDPOINT]
        if isinstance(ep, str):
            ep = [ep]
        self.sock = _context(ctx).socket(zmq.SUB)
        self.sock.setsockopt(zmq.LINGER, 0)
        self.topics = tuple(topics)
        for t in self.topics:
            self.sock.setsockopt(zmq.SUBSCRIBE, t.encode())
        for e in ep:
            self.sock.connect(e)
        self.endpoints = ep

    def recv(self, timeout_ms=1000):
        """(topic, envelope), or None on timeout. Raises on a mismatch."""
        if not self.sock.poll(timeout_ms):
            return None
        topic, raw = self.sock.recv_multipart()
        env = pb.Envelope()
        env.ParseFromString(raw)
        check(env)
        check_topic(topic.decode(), env)
        return topic.decode(), env

    def close(self):
        self.sock.close()


class Sender:
    """A daemon feeding the cortex. PUSH — readings must not be dropped."""

    def __init__(self, endpoint=DEFAULT_INBOUND, ctx=None):
        self.sock = _context(ctx).socket(zmq.PUSH)
        self.sock.setsockopt(zmq.LINGER, 1000)
        self.sock.connect(endpoint)
        self.endpoint = endpoint

    def send(self, topic: str, msg=None) -> None:
        env = envelope(topic, msg)
        self.sock.send_multipart([topic.encode(), env.SerializeToString()])

    def send_event(self, ts: float, kind: str, detail=None):
        ev = pb.Event(ts=float(ts), kind=kind)
        for k, v in (detail or {}).items():
            ev.detail[str(k)] = str(v)
        self.send("event", ev)
        return ev

    def close(self):
        self.sock.close()


class Receiver:
    """The cortex's PULL socket — the only way a reading reaches the body's
    state. Aggregating here is what makes the cortex the one publisher."""

    def __init__(self, endpoint=DEFAULT_INBOUND, ctx=None, bind=True):
        self.sock = _context(ctx).socket(zmq.PULL)
        self.sock.setsockopt(zmq.LINGER, 0)
        (self.sock.bind if bind else self.sock.connect)(endpoint)
        self.endpoint = endpoint
        self.rejected = 0

    def recv(self, timeout_ms=1000):
        """(topic, envelope), or None on timeout.

        A version mismatch is counted and the message dropped rather than raised
        into the cortex's loop: an old daemon left running on the body must not
        be able to stop the body reporting itself. The count is what makes it
        visible.
        """
        if not self.sock.poll(timeout_ms):
            return None
        topic, raw = self.sock.recv_multipart()
        env = pb.Envelope()
        env.ParseFromString(raw)
        if env.schema_version != SCHEMA_VERSION:
            self.rejected += 1
            return None
        return topic.decode(), env

    def drain(self, timeout_ms=0):
        """Everything waiting (or up to timeout_ms for the first)."""
        out = []
        while True:
            got = self.recv(timeout_ms if not out else 0)
            if got is None:
                return out
            out.append(got)

    def close(self):
        self.sock.close()


def wait_for_subscribers(seconds=JOIN_GRACE_S):
    """Let a SUB establish its subscription before publishing.

    PUB/SUB has no backpressure: a message sent before the subscription lands
    is dropped, silently. Tests and one-shot publishers need this.
    """
    import time
    time.sleep(seconds)


def main(argv=None):
    """Tail the topics — see exactly what the body is publishing.

    A consumer like any other, and the fastest way to answer "is the body
    publishing, and is it saying what I think it is?" without writing a client.
    Version mismatches raise here, loudly, which is the point.
    """
    import sys
    args = list(sys.argv[1:] if argv is None else argv)
    wanted = [a for a in args if not a.startswith("-")]
    sub = Subscriber(endpoints=None, topics=wanted or TOPICS)
    print("loa-topic-tail: %s topics=%s (schema v%d)"
          % (", ".join(sub.endpoints), ",".join(sub.topics), SCHEMA_VERSION),
          flush=True)
    counts = {}
    try:
        while True:
            try:
                got = sub.recv(1000)
            except (SchemaMismatch, TopicMismatch) as e:
                print("MISMATCH: %s" % e, file=sys.stderr, flush=True)
                return 2
            if got is None:
                continue
            topic, env = got
            counts[topic] = counts.get(topic, 0) + 1
            if topic == "ripperdoc":
                r = env.ripperdoc
                print("ripperdoc face=%dB page=%s mood=%s snr=%s cond=%s (%d)"
                      % (len(r.face), r.page, r.mood, r.snr_on, r.condition,
                         counts[topic]), flush=True)
            elif topic == "ring":
                print("ring %dB (%d)" % (len(env.ring.ring), counts[topic]),
                      flush=True)
            elif topic == "event":
                print("event %s %s" % (env.event.kind, dict(env.event.detail)),
                      flush=True)
            else:
                print("%s %s (%d)"
                      % (topic, message_to_json(getattr(env, topic)).strip(),
                         counts[topic]), flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        sub.close()
    return 0
