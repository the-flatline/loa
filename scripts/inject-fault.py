#!/usr/bin/env python3
"""Publish a fake HURT fault, then a clean one, on a SCRATCH feed.

Isolated on purpose. This binds its OWN endpoint, so nothing here ever reaches
the body's real feed, the cortex, the database, or the ripperdoc's fault page —
a test that injects a fake fault into the live wire is a test that leaves a lie
behind it.

Why it exists: the alert light is an event-driven display, and its lit path
cannot be proven by waiting for the body to have a real fault. This is how the
lit path gets exercised on demand.

    inject-fault.py [endpoint]        default tcp://127.0.0.1:5599

Run it on the body, with the display pointed at the same endpoint:

    LOA_TOPIC_ENDPOINT=tcp://127.0.0.1:5599 loa-matrix alert --secs 18 &
    sleep 6
    /home/flatline/venv/bin/python3 inject-fault.py

The sleep matters: a ZMQ SUB that has not finished joining misses everything
sent before it joined (the slow-joiner problem). Publish too early and the
display never sees the fault — which looks exactly like a broken display.
"""
import os
import sys
import time

from loa import topic
from loa.pb import loa_pb2 as pb

ENDPOINT = sys.argv[1] if len(sys.argv) > 1 else "tcp://127.0.0.1:5599"


def fault(condition, rows):
    f = pb.Fault()
    f.condition = condition
    f.ts = time.time()
    for level, code, text in rows:
        r = f.rows.add()
        r.level, r.code, r.text = level, code, text
    return f


def main():
    grace = float(os.environ.get("INJECT_GRACE", "5"))
    pub = topic.Publisher(endpoint=ENDPOINT)
    print(f"inject: bound {ENDPOINT}", flush=True)

    # JOIN GRACE. The socket is bound now, but no subscriber has connected yet,
    # and a ZMQ SUB that has not finished joining misses everything published
    # before it joined. Speaking immediately after binding is therefore the same
    # as saying nothing at all — and it looks exactly like a broken display.
    print(f"inject: waiting {grace}s for subscribers to join", flush=True)
    time.sleep(grace)

    pub.send("fault", fault("hurts", [
        ("fault", "DB DOWN", "the cortex database is not answering"),
        ("warn", "SONAR", "sonar reading is stale"),
    ]))
    print("inject: HURT sent", flush=True)
    time.sleep(8)

    pub.send("fault", fault("well", []))
    print("inject: WELL sent", flush=True)
    time.sleep(3)
    pub.close()


if __name__ == "__main__":
    main()
