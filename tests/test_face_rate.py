"""The face has its own clock, and the state topics do not ride it.

Two clocks, ONE socket, one thread. The rate split is the whole change: the
face is pixels and gets a frame rate; the state topics are readings that arrive
at 1Hz from the daemons and gain nothing from being republished faster. Before
this, one 500ms tick paced everything — including the panel, which is why the
glass could never be smooth.

These tests exist because a green suite could not see it: every other test calls
`_publish_all` or `_build` directly and never runs the loop.
"""
import time
from collections import Counter

import loa.cortex.__main__ as cortexd


def test_the_face_is_the_frame_topic_and_only_it():
    """The split must partition the tick exactly. A topic in BOTH clocks is a
    topic published twice a frame; a topic in NEITHER is a topic that silently
    stops arriving, which looks like a dead daemon."""
    assert cortexd.FACE_TOPIC == "ripperdoc"        # the face rides here
    assert cortexd.FACE_TOPIC in cortexd.TICK_TOPICS
    assert cortexd.FACE_TOPIC not in cortexd.STATE_TOPICS
    assert set(cortexd.STATE_TOPICS) | {cortexd.FACE_TOPIC} == set(cortexd.TICK_TOPICS)


def test_face_period_is_a_setting_and_zero_means_uncapped(monkeypatch):
    monkeypatch.delenv(cortexd.FACE_TOPIC_FPS_ENV, raising=False)
    assert abs(cortexd.face_period() - 1.0 / cortexd.DEFAULT_FACE_FPS) < 1e-9
    monkeypatch.setenv(cortexd.FACE_TOPIC_FPS_ENV, "240")
    assert abs(cortexd.face_period() - 1.0 / 240) < 1e-9
    monkeypatch.setenv(cortexd.FACE_TOPIC_FPS_ENV, "0")
    assert cortexd.face_period() == 0.0             # flat out, no sleep at all
    monkeypatch.setenv(cortexd.FACE_TOPIC_FPS_ENV, "not a number")
    assert abs(cortexd.face_period() - 1.0 / cortexd.DEFAULT_FACE_FPS) < 1e-9


class _StubPub:
    """Records what was published. No socket, no cortex, no state."""

    def __init__(self, **kw):
        self.sent = []

    def send(self, topic, msg=None):
        self.sent.append(topic)

    def publish_event(self, *a, **kw):
        pass


def test_the_loop_publishes_the_face_far_faster_than_the_state(monkeypatch):
    """The measured claim, not the shape of the code: over 0.3s at 100fps we
    expect ~30 face frames and a state tick or two at TICK_S=0.5."""
    monkeypatch.setattr(cortexd.topic_mod, "Publisher", _StubPub)
    monkeypatch.setenv(cortexd.FACE_TOPIC_FPS_ENV, "100")
    endpoint = "tcp://stub:9999"
    try:
        pub = cortexd.start_publishing(endpoint=endpoint)
        time.sleep(0.3)
    finally:
        stop = cortexd._PUB[endpoint]["stop"]
        stop.set()
        cortexd._PUB.pop(endpoint, None)
    counts = Counter(pub.sent)
    faces, state = counts[cortexd.FACE_TOPIC], sum(
        counts[t] for t in cortexd.STATE_TOPICS)
    assert faces >= 20, "the face did not run at its own rate: %d" % faces
    assert faces > state, (
        "the state rode the frame clock: %d face, %d state" % (faces, state))
    # and the state topics still went out, so nobody was left blind
    assert state >= 1, "the state clock stopped ticking"
