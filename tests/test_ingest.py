"""ingest — readings arriving by topic get merged into the body's state.

The daemons no longer write the cortex's database; they PUBLISH on their own
topic and this is the half that reads them back. What it must not do is let one
daemon's message clobber a field it never measured — merging is by PRESENCE, and
these tests are the proof.
"""
import time

import pytest

from loa import topic
from loa.cortex import __main__ as cortexd
from loa.cortex import state as cortex
from loa.cortex import store


@pytest.fixture
def body():
    cortex.reset_for_tests()
    cortex.boot(store.MemoryStore())
    rx, stop = cortexd.start_ingesting(endpoint="inproc://ingest")
    tx = topic.Sender(endpoint="inproc://ingest")
    topic.wait_for_subscribers(0.3)
    yield tx
    stop()
    tx.close()
    rx.close()
    cortex.reset_for_tests()


def _settle(field, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cortex.get_state().get(field) not in (None, 0, False):
            return True
        time.sleep(0.02)
    return False


def test_a_reading_lands_in_the_state(body):
    body.send("sonar", topic.partial("sonar", {"snr_cm": 41.0, "snr_count": 3}))
    assert _settle("snr_cm"), "the ingest never merged the sonar reading"
    st = cortex.get_state()
    assert st["snr_cm"] == 41.0
    assert st["snr_count"] == 3


def test_a_reading_does_not_clobber_what_it_never_measured(body):
    """The failure presence exists to prevent: a sonar ping carrying nothing
    about the PIR must not zero the PIR."""
    cortex.set_state({"pir_high": True})
    body.send("sonar", topic.partial("sonar", {"snr_cm": 12.0}))
    assert _settle("snr_cm")
    assert cortex.get_state()["pir_high"] is True


def test_two_topics_merge_into_one_body(body):
    body.send("pir", topic.partial("pir", {"pir_high": True, "pir_count": 9}))
    body.send("weather", topic.partial("weather", {"temp_c": 19.5}))
    assert _settle("pir_count") and _settle("temp_c")
    st = cortex.get_state()
    assert st["pir_high"] is True
    assert st["pir_count"] == 9
    assert st["temp_c"] == 19.5


def test_a_baro_reading_becomes_a_stored_sample(body):
    """The driver does not write the store — it publishes, and the cortex
    records. That keeps one writer for the store."""
    s = store.MemoryStore()
    cortex.set_store(s)
    body.send("baro", topic.partial("baro", {"pressure_hpa": 1012.5,
                                             "baro_temp_c": 20.0,
                                             "baro_ts": 1234.0}))
    deadline = time.time() + 3
    while time.time() < deadline and not s.baro_samples():
        time.sleep(0.05)
    assert s.baro_samples(), "the cortex never recorded the baro sample"
    assert s.baro_samples()[-1][1] == pytest.approx(1012.5)


def test_a_record_arrives_as_a_record(body):
    s = store.MemoryStore()
    cortex.set_store(s)
    body.send("event", topic.event(time.time(), "sense",
                                   {"svc": "motion", "gpio": 17}))
    deadline = time.time() + 4
    while time.time() < deadline and not s.events:
        time.sleep(0.05)
    assert s.events, "the record never reached the store"
    assert s.events[0]["kind"] == "sense"


def test_a_stale_schema_from_a_daemon_is_refused_loudly(body):
    """An old daemon left running must not be able to poison the state."""
    from loa.pb import loa_pb2 as pb
    stale = pb.Envelope(schema_version=topic.SCHEMA_VERSION - 1)
    stale.sonar.cm = 99.0
    body.sock.send_multipart([b"sonar", stale.SerializeToString()])
    time.sleep(0.4)
    assert cortex.get_state()["snr_cm"] is None, "a stale message got through"
