"""Probe: which topics arrive, and does any ripperdoc message carry a face?
Run from the repo root: .venv/bin/python scripts/feed_probe.py
"""
import collections
import os
import time

os.environ.setdefault("LOA_TOPIC_ENDPOINT", "tcp://192.168.1.200:5556")

from loa import topic as t  # noqa: E402

sub = t.Subscriber(endpoints=[os.environ["LOA_TOPIC_ENDPOINT"]],
                   topics=list(t.TOPICS))
counts = collections.Counter()
faces = []
t0 = time.time()
while time.time() - t0 < 8.0:
    got = sub.recv(500)
    if got is None:
        continue
    name, env = got
    counts[name] += 1
    if name == "ripperdoc" and env.ripperdoc.HasField("face"):
        faces.append(len(env.ripperdoc.face))
print("elapsed     : %.1fs" % (time.time() - t0))
print("topic counts:", dict(counts))
print("face lens   :", faces[:10], "distinct:", sorted(set(faces)))
