"""the face goes UP on the topic — on change only, and it never raises.

Replaced on 2026-09-12: the file this replaces tested a 1KB file in /dev/shm that
a consumer read behind the publisher's back, with the file's mtime standing in
for an event. That path is gone — no file, no mtime, no second data path. The
frame now rides inside a Ripperdoc event and the cortex publishes it.
"""
from loa import face, oled, topic


class _Sink:
    """Stands in for the Sender: records what the daemon put on the wire."""

    def __init__(self):
        self.sent = []

    def send(self, name, msg):
        self.sent.append((name, msg))


def _frame(fill=0):
    f = face.Frame()
    f.buf[:] = bytes([fill]) * (face.WIDTH * face.PAGES)
    return f


def test_identical_frames_are_not_republished(monkeypatch):
    """The daemon redraws 4-30x a second; a status page is usually identical
    frame to frame. Sending the same bytes is work the cortex would then have to
    publish at the tick anyway."""
    sink = _Sink()
    monkeypatch.setitem(oled._OUT, "sock", sink)
    monkeypatch.setitem(oled._LAST_FACE, "buf", None)
    f = _frame(0x11)
    oled._publish_face(f)
    oled._publish_face(f)
    oled._publish_face(f)
    assert len(sink.sent) == 1


def test_the_frame_rides_inside_the_ripperdoc_event(monkeypatch):
    """One protobuf event with the raw bytes as a field — not a side-channel,
    not multipart, and nothing encoded."""
    sink = _Sink()
    monkeypatch.setitem(oled._OUT, "sock", sink)
    monkeypatch.setitem(oled._LAST_FACE, "buf", None)
    oled._publish_face(_frame(0x5A))
    name, msg = sink.sent[0]
    assert name == "ripperdoc"
    assert len(msg.face) == 1024
    assert msg.face[0] == 0x5A
    env = topic.envelope("ripperdoc", msg)
    assert env.WhichOneof("body") == "ripperdoc"


def test_a_changed_frame_is_published(monkeypatch):
    sink = _Sink()
    monkeypatch.setitem(oled._OUT, "sock", sink)
    monkeypatch.setitem(oled._LAST_FACE, "buf", None)
    f = _frame(0x00)
    oled._publish_face(f)
    f.buf[700] ^= 0xFF
    oled._publish_face(f)
    assert len(sink.sent) == 2


def test_a_failed_publish_does_not_raise(monkeypatch):
    """A feed failure is not a reason for the face to stop drawing. A body with
    no publisher must still have a face."""
    class Boom:
        def send(self, *a, **k):
            raise RuntimeError("socket gone")
    monkeypatch.setitem(oled._OUT, "sock", Boom())
    monkeypatch.setitem(oled._LAST_FACE, "buf", None)
    oled._publish_face(_frame(0x77))        # must not raise


def test_no_publisher_is_not_a_crash(monkeypatch):
    """Before the sender is made (or on the bench), there is simply no sink."""
    monkeypatch.setitem(oled._OUT, "sock", None)
    monkeypatch.setitem(oled._LAST_FACE, "buf", None)
    oled._publish_face(_frame(0x03))
