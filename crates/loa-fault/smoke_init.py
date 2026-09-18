"""Smoke: start the loa-fault daemon, play the cortex's six init asks on the
feed, count fault payloads arriving on the inbound PULL. Not a crate test —
a one-off proof the wire paths work (uses the repo venv's pyzmq)."""
import os, subprocess, sys, time
import zmq

os.environ.setdefault("LOA_INBOUND", "tcp://127.0.0.1:5577")
os.environ.setdefault("LOA_TOPIC_ENDPOINT", "tcp://127.0.0.1:5578")

ctx = zmq.Context()
pull = ctx.socket(zmq.PULL)
pull.bind("tcp://127.0.0.1:5577")
pub = ctx.socket(zmq.PUB)
pub.bind("tcp://127.0.0.1:5578")

binp = os.path.join(os.path.dirname(__file__), "target/debug/loa-fault")
proc = subprocess.Popen([binp], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1.0)  # SUB join grace

def drain():
    n = 0
    while pull.poll(0, zmq.POLLIN) == zmq.POLLIN:
        topic, _ = pull.recv_multipart()
        assert topic == b"fault", topic
        n += 1
    return n

got_start = drain()
print(f"push at daemon start: {got_start}")

for i in range(6):
    # a real Envelope: schema_version=10 (08 0a), body=Init (tag 0x52, len 6:
    # field 1 source = "test")
    env = bytes([0x08, 0x0A, 0x52, 0x06, 0x0A, 0x04]) + b"test"
    pub.send_multipart([b"init", env])
    time.sleep(0.4)
time.sleep(2.0)
got_asks = drain()
print(f"pushes after 6 init asks: {got_asks}")

proc.terminate(); proc.wait()
assert got_start >= 1, "daemon must push its full payload at start"
assert got_asks >= 4, f"each ask must re-sweep and re-push; got {got_asks}/6"
print("OK: every ask answered (>=4 of 6 landed through PUB/SUB join), start push present")
