"""record — the recorder consumer (loa-record).

Subscribes to the topic and writes RECORDS. It is a consumer, not a cortex
output: the cortex publishes once and keeps no private copy for the database.
Three outputs is three things that can drift.

Records only. Live state is NOT written to disk — it lives in RAM, per doctrine,
and a brown-out must not leave a body claiming a mood that ended with the power
(the alarm that survived a reset, 2026-09-12).

The stored form is the schema's canonical JSON: snake_case field names, every
field present. Transcoding through the .proto means the column cannot disagree
with the bus about what a field is called or what a reading means.

Events carry scalars only (see proto/loa.proto): a bytes field would be silently
base64-encoded by the canonical JSON mapping and smuggle an encoding into the
database where nobody looks for one. A frame event records the frame's hash.
"""
import json
import sys
import time

from . import cortex
from . import topic


def record_event(event) -> None:
    """Write one Event message to `events`, via the schema mapping.

    The message is transcoded, not hand-decoded: one .proto generates the wire
    form and the stored form, so they cannot fall out of step.

    `detail` holds the event's detail MAP, not the whole message: ts and kind
    have their own columns, and duplicating them inside the JSON would give the
    same fact two places to disagree from.
    """
    whole = topic.event_to_dict(event)
    cortex.log_event(event.kind, whole.get("detail") or {}, ts=event.ts)


def handle(envelope) -> str:
    """One incoming Envelope. Returns what it did, for logging and tests."""
    kind = envelope.WhichOneof("body")
    if kind == "event":
        record_event(envelope.event)
        return "event"
    if kind == "state":
        # State is NOT recorded: live state belongs in RAM. The one exception
        # worth recording is the baro trend, which is history by definition —
        # and it travels as an event from the weather daemon, not as a copy of
        # the state stream.
        return "state-ignored"
    if kind == "twin":
        # Frames belong to the live feed. Records get hashes, never pixels.
        return "twin-ignored"
    return "unknown"


def main(argv=None) -> int:
    endpoints = None
    if argv:
        endpoints = [a for a in argv if not a.startswith("-")]
    sub = topic.Subscriber(endpoints=endpoints)
    if "-v" in (argv or []):
        print("loa-record: subscribed to %s" % ",".join(sub.endpoints), flush=True)
    counts = {}
    try:
        while True:
            env = sub.recv(1000)
            if env is None:
                continue
            try:
                did = handle(env)
            except Exception as e:                                  # noqa: BLE001
                print("loa-record: %s: %s" % (type(e).__name__, e),
                      file=sys.stderr, flush=True)
                continue
            counts[did] = counts.get(did, 0) + 1
    except KeyboardInterrupt:
        pass
    finally:
        sub.close()
        if "-v" in (argv or []):
            print("loa-record: %s" % counts, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
