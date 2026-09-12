"""oled_daemon — publish on change.

A daemon that rewrites identical bytes 30 times a second is doing work no
reader can observe, and on an uncooled body that work is heat. The bytes only
need writing when they actually differ — same semantics a subscriber wants.

Note: the test counts WRITES rather than comparing mtimes. Two writes inside
the same kernel clock tick share an mtime, so an mtime assertion is flaky by
construction (found the hard way).
"""
import os

import loa.oled_daemon as od
from loa import oled


def _counting_open(writes):
    real_open = open

    def opener(path, mode="r", *a, **kw):
        if "w" in mode:
            writes.append(str(path))
        return real_open(path, mode, *a, **kw)

    return opener


def test_identical_frames_are_not_republished(tmp_path, monkeypatch):
    topic = tmp_path / "face.bin"
    monkeypatch.setattr(od, "OLED_TOPIC", str(topic))
    monkeypatch.setattr(od, "_LAST_FACE", {"buf": None})
    writes = []
    monkeypatch.setattr(od, "open", _counting_open(writes), raising=False)

    f = oled.Frame()                          # known blank: every px unlit
    od._publish_face(f)
    assert writes == [str(topic)], "first publish must land"

    od._publish_face(f)                       # same pixels again
    od._publish_face(f)
    assert len(writes) == 1, "identical frames were rewritten"

    f.px(0, 0, True)                          # now it differs
    od._publish_face(f)
    assert len(writes) == 2, "a changed frame must publish"
    assert topic.read_bytes()[0] & 1, "the change must actually land"


def test_a_failed_publish_does_not_raise(monkeypatch):
    monkeypatch.setattr(od, "OLED_TOPIC", "/proc/definitely/not/writable")
    monkeypatch.setattr(od, "_LAST_FACE", {"buf": None})
    od._publish_face(oled.Frame())             # must not raise
    assert os.path.exists("/proc")             # sanity: we are still here
