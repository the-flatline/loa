"""The console consumes the TOPIC. There is no HTTP data path.

These tests replaced a set that asserted the console reads /state and /live over
HTTP. That contract is gone: Divv's shape is one source and many consumers, and a
console that quietly falls back to a poll is not on pub/sub no matter what the
code around it says.

The stub feed is deliberate: the point is what the console does with the data it
receives, and a real socket in a unit test would only prove that zmq works.
"""
import base64

import loa.ripperdoc as rd
from loa import topic
from loa.cortex import face


class _StubFeed:
    """A Feed that has already received what the test wants it to have."""

    def __init__(self, state=None, face_bytes=b"", ring_bytes=b"",
                 age=0.0, error=None):
        self._state = state or {}
        self._face = face_bytes
        self._ring = ring_bytes
        self._age = age
        self.error = error

    def state_copy(self):
        return dict(self._state)

    def frames(self):
        return self._face, self._ring

    def age(self):
        return self._age


def _no_http(monkeypatch):
    """Fail the test if the console reaches for the network at all."""
    def forbidden(path):
        raise AssertionError("the console fetched %s over HTTP" % path)
    monkeypatch.setattr(rd, "_get", forbidden)


def test_the_console_takes_its_state_from_the_feed(monkeypatch):
    _no_http(monkeypatch)
    monkeypatch.setattr(rd, "FEED", _StubFeed(state={
        "mood": "alarmed", "ring_state": "alarm", "oled_mode": "ripperdoc",
        "ripperdoc_page": "power", "condition": "hurts",
        "power": {"3V3_SYS_V": 3.31, "throttled": 0},
    }))
    st = rd.body_state()
    assert st["mood"] == "alarmed" and st["ring_state"] == "alarm"
    line = rd.fetch_sense()
    assert "alarmed" in line and "power" in line
    assert "3.31V" in line, "power must come off the feed, not off local hardware"


def test_a_silent_feed_says_so_rather_than_showing_the_last_picture(monkeypatch):
    """A console that shows its last reading forever is the ghost temperature
    with a different coat on."""
    _no_http(monkeypatch)
    monkeypatch.setattr(rd, "FEED", _StubFeed(state={"mood": "calm"},
                                              age=rd._FEED_STALE_S + 1))
    line = rd.fetch_sense()
    assert "NO FEED" in line and "calm" not in line


def test_a_schema_mismatch_is_reported_not_guessed_at(monkeypatch):
    """A publisher and a subscriber that disagree must fail loudly. Guessing is
    how a hex frame decoded as base64 drew twelve wrong pixels and looked like a
    hardware fault."""
    _no_http(monkeypatch)
    monkeypatch.setattr(rd, "FEED",
                        _StubFeed(error="schema v9 != v%d" % topic.SCHEMA_VERSION))
    line = rd.fetch_sense()
    assert "FEED ERROR" in line and "schema" in line


def test_the_ring_frame_is_decoded_as_hex(monkeypatch):
    """/live sent the ring hex-encoded; the feed sends raw bytes. Either way it
    is never base64 — that assumption is what drew twelve wrong LEDs."""
    ring = bytes([0, 0, 216] * 24)
    monkeypatch.setattr(rd, "FEED", _StubFeed(ring_bytes=ring))
    _face, ring_b = rd.feed().frames()
    assert ring_b == ring and len(ring_b) == 72
    assert "rgb(0,0,216)" in rd.ring_art_bytes(ring_b)
    # and the old hex shape still round-trips for anything left using it
    assert bytes.fromhex(ring.hex()) == ring
    assert base64.b64decode(base64.b64encode(ring)) == ring


def test_the_frag_page_draws_the_bodys_seal_from_the_feed(monkeypatch):
    """The vault lives on the loa. The console renders the real page locally, so
    without the seal coming off the feed it drew GONE over a sealed vault."""
    drawn = []
    monkeypatch.setattr(face.amiga, "draw",
                        lambda frame, text, x, y, size=8, **kw: drawn.append(text))
    # `page`, not `ripperdoc_page`: the state key was renamed to match the wire
    # field on the ripperdoc topic, so there is one name for it end to end.
    st = {"page": "frag",
          "frag": {"sealed": True, "entries": 3, "access_count": 6}}
    face.Ripperdoc().draw_state(face.Frame(), 0.0, st)
    assert "SEALED" in drawn and "GONE" not in drawn
    assert "N003" in drawn and "A006" in drawn


def test_the_data_path_cannot_quietly_regress_to_http():
    """The console's live data path is the topic, and it must stay that way.

    This has already regressed once: the code said "feed" all over it while a
    /state read sat in an action and the Feed parsed a oneof shape the publisher
    no longer sent, so it delivered nothing and looked like a quiet body. The
    guard therefore PARSES the module rather than grepping it — a mention of
    /state in a comment or a docstring must not satisfy the check, and a string
    that merely looks like a call must not trip it. What is forbidden is the
    AST: no `_get("/state")`, `_get("/sense")` or `_get("/live")` call anywhere.
    Commands go over `_post()`; reads come off the feed.
    """
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(rd.__file__).read_text())
    forbidden = {"/state", "/sense", "/live"}
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "_get"):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and arg.value in forbidden:
                offenders.append((arg.value, node.lineno))
    assert not offenders, (
        "the console is reading live data over HTTP again: %s — the data path "
        "is the topic, never a poll" % offenders)
