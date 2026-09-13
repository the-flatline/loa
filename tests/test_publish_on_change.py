"""The picture goes UP from the BRAIN — and the glass gets those exact bytes.

Replaced 2026-09-13. This file used to test `oled._publish_face`: the face daemon
rendered its own frame and pushed it UP, with a 250ms re-assert to paper over a
cortex restart. That is a limb telling the brain what it drew, and it is the
thing Divv's correction removes — the cortex knows the state it told the OLED to
render, so the cortex renders it.

So the tests here are the invariant, not the plumbing: the cortex renders a
1024-byte face and a 72-byte ring from ITS OWN state, the topic carries exactly
those bytes, and a brand-new cortex publishes a full frame on its first tick
with no re-assert hack of any kind.
"""
import ast
import pathlib
import time

from loa import cortex, cortexd, frames, topic


class _Pub:
    """Records what the cortex put on the wire, by topic."""

    def __init__(self):
        self.sent = []

    def send(self, name, msg):
        self.sent.append((name, msg))

    def publish_event(self, *a, **k):
        self.sent.append(("event", None))


def _fresh(st=None):
    """A state with a sweep heard, so the renderers do not read NO SWEEP."""
    st = dict(st or cortex.get_state())
    st["fault_ts"] = time.time()
    return st


def test_the_tick_publishes_the_rendered_frames():
    pub = _Pub()
    cortexd._publish_all(pub, _fresh())
    by = {name: msg for name, msg in pub.sent}
    assert len(by["ripperdoc"].face) == 1024
    assert len(by["ring"].ring) == 72


def test_the_face_on_the_topic_IS_the_face_the_renderer_made():
    """Byte-compare. The topic carries the renderer's output, not a second
    render and not a size-checked approximation of one."""
    pub = _Pub()
    cortexd._publish_all(pub, _fresh())
    by = {name: msg for name, msg in pub.sent}
    assert bytes(by["ripperdoc"].face) == cortexd._FACE
    assert bytes(by["ring"].ring) == cortexd._RING
    assert len(cortexd._FACE) == 1024 and len(cortexd._RING) == 72


def test_off_is_exactly_a_blank_panel():
    """The strongest byte check there is: a deterministic state makes the
    published bytes exactly predictable, so nothing can be quietly re-rendered
    on the way out."""
    cortex.reset_for_tests()
    cortex.set_state({"oled_mode": "off"})
    st = _fresh()
    cortexd._render(st)
    msg = cortexd._build("ripperdoc", st)
    assert bytes(msg.face) == b"\x00" * 1024


def test_a_restarted_cortex_publishes_a_full_face_on_the_first_tick(monkeypatch):
    """No re-assert hack. A brand-new cortex has no last frame and no history:
    one tick is enough, because the tick RENDERS rather than re-sends."""
    monkeypatch.setattr(cortexd, "_RENDER", {
        "face": frames.FaceRenderer(), "ring": frames.RingRenderer()})
    monkeypatch.setattr(cortexd, "_FACE", b"")
    monkeypatch.setattr(cortexd, "_RING", b"")
    pub = _Pub()
    cortexd._publish_all(pub, _fresh())
    by = {name: msg for name, msg in pub.sent}
    assert len(by["ripperdoc"].face) == 1024
    assert len(by["ring"].ring) == 72


# -- the AST guards: a display does not render, and a renderer reads no file -- #

RENDERERS = ("face", "frames", "oled", "ring", "animations", "render", "amiga")


def test_no_renderer_constructs_a_sender():
    """A renderer that PUSHES UP is a limb informing the brain. The cortex
    renders; a display blits; neither reaches for the wire's up-leg."""
    root = pathlib.Path(__file__).resolve().parent.parent / "loa"
    offenders = []
    for name in RENDERERS:
        tree = ast.parse((root / f"{name}.py").read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                called = (func.id if isinstance(func, ast.Name)
                          else func.attr if isinstance(func, ast.Attribute)
                          else "")
                if called.endswith("Sender"):
                    offenders.append(f"{name}.py:{node.lineno} {called}()")
    assert not offenders, (
        "a renderer is pushing frames up the wire: %s" % offenders)


def test_no_renderer_reads_dev_shm():
    """The sweep's file has exactly ONE reader (the cortex's ingest). A renderer
    reading it draws something the caller did not hand it — and the failure mode
    is a plausible, wrong picture, not an exception.

    Parsed, not grepped: the PATH may appear in a comment explaining why it is
    not read here (it does, in face.py) — what is forbidden is the string in
    code.
    """
    root = pathlib.Path(__file__).resolve().parent.parent / "loa"
    offenders = []
    for name in RENDERERS:
        tree = ast.parse((root / f"{name}.py").read_text())
        docstrings = set()
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if isinstance(body, list) and body:
                first = body[0]
                if (isinstance(first, ast.Expr)
                        and isinstance(first.value, ast.Constant)
                        and isinstance(first.value.value, str)):
                    docstrings.add(id(first.value))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and id(node) not in docstrings
                    and "/dev/shm" in node.value):
                offenders.append(f"{name}.py:{node.lineno}")
    assert not offenders, (
        "these renderers read /dev/shm instead of the state handed in: %s"
        % offenders)


def test_the_init_is_not_a_state():
    """A control message is not a state: it must never ride the tick, or the
    daemons would re-send their whole payload twice a second."""
    assert "init" in topic.TOPICS
    assert "init" not in cortexd.TICK_TOPICS
