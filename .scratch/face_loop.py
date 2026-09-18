#!/usr/bin/env python3
"""Drive the face daemon end-to-end: publish state on the feed, collect what
it pushes back on the inbound port. Scratch harness, not a repo test."""
import sys, time, threading
import zmq
sys.path.insert(0, ".")
import loa.pb.loa_pb2 as p

FEED = "tcp://127.0.0.1:5596"
INBOUND = "tcp://127.0.0.1:5595"

ctx = zmq.Context()

pub = ctx.socket(zmq.PUB)
pub.bind(FEED)

pull = ctx.socket(zmq.PULL)
pull.bind(INBOUND)  # the daemon's push target; fail fast if the cortex owns it
time.sleep(0.3)

def pub_env(topic, body):
    e = p.Envelope(); e.schema_version = 10
    # set the right body field
    field = {"Ripperdoc": "ripperdoc", "Pir": "pir", "Sonar": "sonar",
             "Weather": "weather", "Fault": "fault", "Power": "power"}[body.DESCRIPTOR.name]
    getattr(e, field).CopyFrom(body)
    pub.send_multipart([topic.encode(), e.SerializeToString()])

def state(mode, page=None, cond="well", text=None):
    m = p.Ripperdoc(); m.oled_mode = mode; m.condition = cond
    if page: m.page = page
    if text: m.oled_text = text
    m.ts = time.time()
    pub_env("state", m)

def fault(rows, cond):
    m = p.Fault(); m.condition = cond; m.ts = time.time()
    for lvl, code in rows:
        r = m.rows.add(); r.level = lvl; r.code = code; r.face = code
    pub_env("fault", m)

def sonar(cm):
    m = p.Sonar(); m.cm = cm; m.count = 1; m.ts = time.time()
    pub_env("sonar", m)

def power():
    m = p.Power(); m.ts = time.time()
    m.rails["3V3_SYS_V"] = 3.31; m.rails["EXT5V_V"] = 5.02
    m.rails["throttled"] = 983044.0
    pub_env("power", m)

def weather():
    m = p.Weather(); m.temp_c = 24.5; m.hum_pct = 41.0; m.ts = time.time()
    pub_env("weather", m)

def pir():
    m = p.Pir(); m.high = True; m.count = 7; m.on_ts = time.time() - 3
    m.last_ts = time.time() - 1.5; m.ts = time.time()
    pub_env("pir", m)

got = []
stop = threading.Event()
def reader():
    pull.RCVTIMEO = 200
    while not stop.is_set():
        try:
            parts = pull.recv_multipart()
        except zmq.Again:
            continue
        topic = parts[0].decode()
        e = p.Envelope(); e.ParseFromString(parts[1])
        got.append((topic, e))

t = threading.Thread(target=reader); t.start()

# pace: the SUB needs its subscription to land before the first news
state("scope")
time.sleep(0.4)
for i in range(3):
    state("scope"); time.sleep(0.15)
sonar(42.0); weather(); power(); pir()
for page in ["sensors", "pir", "snr", "temp", "frag", "power", "fault"]:
    state("ripperdoc", page=page); time.sleep(0.2)
state("text", text="FLATLINE"); time.sleep(0.3)
state("showoff"); time.sleep(0.3)
fault([("fault", "OLED LOOP")], "hurts")
state("scope"); time.sleep(0.5)   # hurts must take over the scope face
fault([], "well")
state("scope"); time.sleep(0.5)   # well again: the scope comes back

stop.set(); t.join()

print(f"received {len(got)} pushed frames")
ok = 0
seen_pages, modes_seen = set(), []
for topic, e in got:
    assert topic == "oled", topic
    assert e.schema_version == 10
    assert e.WhichOneof("body") == "ripperdoc"
    face = e.ripperdoc.face
    assert face is not None and len(face) == 1024, len(face) if face else None
    ok += 1
    if e.ripperdoc.page: seen_pages.add(e.ripperdoc.page)
print("all frames valid:", ok)
uniq = len(set(b for _, e in got for b in [e.ripperdoc.face]))
print("distinct frames:", uniq)
ts = [e.ripperdoc.ts for _, e in got]
print("ts monotonic-ish:", all(b >= a for a, b in zip(ts, ts[1:])))
# the last few frames: after 'well' the scope line is back (not all-black)
last = got[-1][1].ripperdoc.face
assert any(b != 0 for b in last), "last frame black"
sys.exit(0 if ok == len(got) and ok > 5 else 1)
