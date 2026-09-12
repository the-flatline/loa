"""oled_daemon — publish on change.

A daemon that rewrites identical bytes 30 times a second is doing work no
reader can observe, and on an uncooled body that work is heat. The file's
mtime is the signal that the frame moved; the bytes only need writing when
they actually differ. Same semantics a subscriber wants.
"""
import os

import loa.oled_daemon as od
from loa import oled


def test_identical_frames_are_not_republished(tmp_path, monkeypatch):
    topic = tmp_path / "face.bin"
    monkeypatch.setattr(od, "OLED_TOPIC", str(topic))
    monkeypatch.setattr(od, "_LAST_FACE", {"buf": None})

    f = oled.Frame()
    oled.Ripperdoc().draw_state(f, 1.0, {"ripperdoc_page": "fault"})

    od._publish_face(f)
    assert topic.exists(), "first publish must land"
    first = topic.stat().st_mtime_ns
    payload = topic.read_bytes()

    od._publish_face(f)                       # same pixels again
    assert topic.stat().st_mtime_ns == first, "identical frame was rewritten"
    assert topic.read_bytes() == payload

    f.px(3, 3, True)                          # now it differs
    od._publish_face(f)
    assert topic.stat().st_mtime_ns != first, "a changed frame must publish"


def test_a_failed_publish_does_not_raise(monkeypatch):
    monkeypatch.setattr(od, "OLED_TOPIC", "/proc/definitely/not/writable")
    monkeypatch.setattr(od, "_LAST_FACE", {"buf": None})
    f = oled.Frame()
    od._publish_face(f)                        # must not raise
    assert os.path.exists("/proc")             # sanity: we are still here
