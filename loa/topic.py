"""topic — the one source. Publish once, subscribe everywhere.

The shape, settled with Divv 2026-09-12:

    senses + fault  --push-->  loa-cortex  --PUB-->  THE TOPIC
                                                     (protobuf, ZMQ)
                                                        |
         subscribers, each independent, each one job: <-+
           loa-ring, loa-oled   (subscribe, do not poll)
           recorder             (sqlite: records only)
           state/twin           (RAM; serves /state + /health)
           web bridge           (forwards raw protobuf to the browser)

The cortex does NOT fan out private copies for the feed, RAM and the DB. Three
outputs is three things that can drift, and drift-that-looks-right is the exact
failure that cost an hour of phantom-LED hunting: a hex ring frame base64
decoded into a right-SIZED, entirely wrong answer.

So: one schema (proto/loa.proto), one publisher, and every consumer subscribes
to what it needs. Adding a consumer never touches the body.

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
import zmq

from google.protobuf import json_format

from .pb import loa_pb2 as pb

#: Bump on ANY change to proto/loa.proto. Publisher and subscriber check it, so
#: a body and a console that disagree fail on arrival instead of misreading.
#: v2: State fields gained explicit presence so a daemon can publish a PARTIAL
#: reading without clobbering the fields it does not own.
#: v3: State carries power/faults/frag so a consumer never has to reach back
#: over HTTP for what the feed should already be telling it.
#: v4: State carries the baro trend and series — the website's sparkline was the
#: last thing still being fetched, and it must not die to make a point.
SCHEMA_VERSION = 4

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
    it already had. A state snapshot must state every field."""
    return json_format.MessageToJson(
        msg, preserving_proto_field_name=True, indent=indent,
        always_print_fields_with_no_presence=True)


def event_to_dict(msg) -> dict:
    """An Event message as a plain dict, through the same canonical mapping.

    Uses dataclass-style field names, not the default camelCase, so a stored
    record and the code that reads it agree about what a field is called."""
    return json.loads(message_to_json(msg))


def state_fields_present(msg: pb.State) -> dict:
    """Only the fields this message actually carries.

    Every scalar State field is `optional`, so presence is the difference
    between "I measured false" and "I am not talking about that". Merging a
    partial reading without this test is how a motion daemon clobbers the mood.
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


def state_to_message(state: dict) -> pb.State:
    """A state dict to a State message, through the schema mapping.

    No hand-written field list to fall out of step with the .proto. Unknown
    keys are ignored so internal extras (counters, caches) cannot break the
    publish path."""
    return json_format.ParseDict(state, pb.State(),
                                 ignore_unknown_fields=True)


def message_to_state(msg: pb.State) -> dict:
    """The inverse — the same one mapping, so the two cannot disagree.

    Defaults are printed (see message_to_json): a consumer must be able to read
    oled_dim=False as a fact, not as silence."""
    return json_format.MessageToDict(
        msg, preserving_proto_field_name=True,
        always_print_fields_with_no_presence=True)


# --------------------------------------------------------------------------- #
# sockets
# --------------------------------------------------------------------------- #

def _context(ctx=None):
    return ctx or zmq.Context.instance()


def _envelope(schema_version=SCHEMA_VERSION) -> pb.Envelope:
    return pb.Envelope(schema_version=schema_version)


def check(envelope) -> None:
    """Raise SchemaMismatch unless this message is our schema."""
    if envelope.schema_version != SCHEMA_VERSION:
        raise SchemaMismatch(
            "wire schema v%d, this build speaks v%d — refusing to guess at the "
            "fields" % (envelope.schema_version, SCHEMA_VERSION))


class Publisher:
    """The cortex's PUB socket. There should be exactly one in the system."""

    def __init__(self, endpoint=DEFAULT_ENDPOINT, ctx=None, bind=True):
        self.sock = _context(ctx).socket(zmq.PUB)
        self.sock.setsockopt(zmq.LINGER, 0)
        (self.sock.bind if bind else self.sock.connect)(endpoint)
        self.endpoint = endpoint
        self.sent = 0

    def send(self, envelope: pb.Envelope) -> None:
        self.sock.send(envelope.SerializeToString())
        self.sent += 1

    def publish_state(self, state: dict):
        env = _envelope()
        env.state.CopyFrom(state_to_message(state))
        self.send(env)
        return env

    def publish_frames(self, face: bytes, ring: bytes):
        env = _envelope()
        env.frames.face = bytes(face)
        env.frames.ring = bytes(ring)
        self.send(env)
        return env

    def publish_event(self, ts: float, kind: str, detail=None):
        env = _envelope()
        env.event.ts = float(ts)
        env.event.kind = kind
        for k, v in (detail or {}).items():
            # string map: events stay scalars, so no bytes field can be
            # silently base64'd into the database
            env.event.detail[str(k)] = str(v)
        self.send(env)
        return env

    def close(self):
        self.sock.close()


class Subscriber:
    """Any consumer. Subscribes to the topic and reads typed messages."""

    def __init__(self, endpoints=None, ctx=None, topics=b""):
        ep = endpoints or [DEFAULT_ENDPOINT]
        if isinstance(ep, str):
            ep = [ep]
        self.sock = _context(ctx).socket(zmq.SUB)
        self.sock.setsockopt(zmq.LINGER, 0)
        self.sock.setsockopt(zmq.SUBSCRIBE, topics)
        for e in ep:
            self.sock.connect(e)
        self.endpoints = ep

    def recv(self, timeout_ms=1000):
        """One Envelope, or None on timeout. Raises SchemaMismatch."""
        if not self.sock.poll(timeout_ms):
            return None
        env = pb.Envelope()
        env.ParseFromString(self.sock.recv())
        check(env)
        return env

    def close(self):
        self.sock.close()


class Sender:
    """A daemon feeding the cortex. PUSH — readings must not be dropped."""

    def __init__(self, endpoint=DEFAULT_INBOUND, ctx=None):
        self.sock = _context(ctx).socket(zmq.PUSH)
        self.sock.setsockopt(zmq.LINGER, 1000)
        self.sock.connect(endpoint)
        self.endpoint = endpoint

    def send(self, envelope: pb.Envelope) -> None:
        self.sock.send(envelope.SerializeToString())

    def send_state(self, state: dict):
        env = _envelope()
        env.state.CopyFrom(state_to_message(state))
        self.send(env)
        return env

    def send_event(self, ts: float, kind: str, detail=None):
        env = _envelope()
        env.event.ts = float(ts)
        env.event.kind = kind
        for k, v in (detail or {}).items():
            env.event.detail[str(k)] = str(v)
        self.send(env)
        return env

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
        """One Envelope, or None on timeout.

        A version mismatch here is counted and the message dropped rather than
        raised into the cortex's loop: an old daemon left running on the body
        must not be able to stop the body reporting itself. The count is what
        makes it visible."""
        if not self.sock.poll(timeout_ms):
            return None
        env = pb.Envelope()
        env.ParseFromString(self.sock.recv())
        if env.schema_version != SCHEMA_VERSION:
            self.rejected += 1
            return None
        return env

    def drain(self, timeout_ms=0):
        """Everything waiting (or up to timeout_ms for the first)."""
        out = []
        while True:
            env = self.recv(timeout_ms if not out else 0)
            if env is None:
                return out
            out.append(env)

    def close(self):
        self.sock.close()


def wait_for_subscribers(publisher, seconds=JOIN_GRACE_S):
    """Let a SUB establish its subscription before publishing.

    PUB/SUB has no backpressure: a message sent before the subscription lands
    is dropped, silently. Tests and one-shot publishers need this."""
    import time
    time.sleep(seconds)
    return publisher


def main(argv=None):
    """Tail the topic — see exactly what the body is publishing.

    A consumer like any other, and the fastest way to answer "is the body
    publishing, and is it saying what I think it is?" without writing a client.
    Version mismatches raise here, loudly, which is the point.
    """
    import sys
    args = list(sys.argv[1:] if argv is None else argv)
    endpoints = [a for a in args if not a.startswith("-")]
    sub = Subscriber(endpoints=endpoints or None)
    print("loa-topic-tail: %s (schema v%d)"
          % (", ".join(sub.endpoints), SCHEMA_VERSION), flush=True)
    try:
        while True:
            try:
                env = sub.recv(1000)
            except SchemaMismatch as e:
                print("SCHEMA MISMATCH: %s" % e, file=sys.stderr, flush=True)
                return 2
            if env is None:
                continue
            which = env.WhichOneof("body")
            if which == "frames":
                print("frames face=%dB ring=%dB"
                      % (len(env.frames.face), len(env.frames.ring)), flush=True)
            elif which == "state":
                print("state mood=%s ring=%s oled=%s condition=%s pir=%s"
                      % (env.state.mood, env.state.ring_state,
                         env.state.oled_mode, env.state.condition,
                         env.state.pir_high), flush=True)
            elif which == "event":
                print("event %s %s" % (env.event.kind, dict(env.event.detail)),
                      flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        sub.close()
    return 0
