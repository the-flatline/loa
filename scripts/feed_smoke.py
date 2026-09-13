"""One-shot smoke check: does the console's feed-backed data source answer?

Run from the repo root:  .venv/bin/python scripts/feed_smoke.py
Builds the console's real Feed against the body and prints what it assembles.
"""
import os
import time

os.environ.setdefault("LOA_TOPIC_ENDPOINT", "tcp://192.168.1.200:5556")

import loa.ripperdoc as rd  # noqa: E402

f = rd.feed()
time.sleep(3.0)
st = f.state_copy()
face_b, ring_b = f.frames()
print("endpoint     :", rd._feed_endpoints())
print("feed error   :", f.error)
print("feed age (s) : %.1f" % f.age())
print("topic counts :", dict(f.counts))
print("page         :", st.get("page"))
print("mood         :", st.get("mood"))
print("face length  :", len(face_b))
print("ring length  :", len(ring_b))
print("state keys   :", sorted(st))
