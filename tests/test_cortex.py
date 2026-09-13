"""cortex — the live state is RAM, and the store is master/slave by class.

These are the rules from the 2026-09-12 conversation, as tests: the state is not
a database row, a settings change is adopted before it is written, a store that
cannot take it is a FAULT rather than a silent revert, and records never block
the tick.
"""
import time

import pytest

from loa.cortex import state as cortex
from loa.cortex import store


@pytest.fixture(autouse=True)
def clean_body():
    cortex.reset_for_tests()
    yield
    cortex.reset_for_tests()


def _flushed(secs=2.5):
    """Let the background flusher run. It wakes every second."""
    time.sleep(secs)


# -- the state is RAM ------------------------------------------------------ #

def test_state_is_ram_not_a_row():
    """No store at all, and the body still answers about itself."""
    cortex.boot(None)
    cortex.set_state({"mood": "pleased", "page": "power"})
    st = cortex.get_state()
    assert st["mood"] == "pleased"
    assert st["page"] == "power"


def test_get_state_is_a_copy():
    """Holding the state must not be a way to change the body."""
    cortex.boot(None)
    st = cortex.get_state()
    st["mood"] = "mutinous"
    assert cortex.get_state()["mood"] == "calm"


def test_unknown_keys_are_ignored():
    cortex.boot(None)
    cortex.set_state({"nonsense": 1, "mood": "calm"})
    assert "nonsense" not in cortex.get_state()


# -- master class: settings ------------------------------------------------ #

def test_settings_are_flushed_to_the_store():
    s = store.MemoryStore()
    cortex.boot(s)
    cortex.set_state({"ring_state": "alarm", "mood": "alarmed"})
    _flushed()
    assert s.settings["ring_state"] == "alarm"
    assert s.settings["mood"] == "alarmed"
    assert cortex.db_down() is False


def test_settings_come_back_at_boot():
    """The one place the store is authoritative: the body waking up."""
    s = store.MemoryStore()
    s.settings = {"ring_state": "off", "page": "fault", "snr_on": False}
    cortex.boot(s)
    st = cortex.get_state()
    assert st["ring_state"] == "off"
    assert st["page"] == "fault"
    assert st["snr_on"] is False


def test_a_counter_does_not_come_back_at_boot():
    """It counts THIS boot. A restored count would claim readings that never
    happened on this body — and would make a fresh boot look busier than it is."""
    s = store.MemoryStore()
    s.settings = {"pir_count": 41}
    cortex.boot(s)
    assert cortex.get_state()["pir_count"] == 0


def test_a_change_is_adopted_before_it_is_written():
    """Hold and flush. A command must not fail because aleph hiccuped."""
    class Slow(store.MemoryStore):
        def save_settings(self, values):
            raise store.StoreUnreachable("aleph is not answering")

    cortex.boot(Slow())
    cortex.set_state({"mood": "tired"})
    assert cortex.get_state()["mood"] == "tired"     # adopted immediately
    _flushed()
    assert cortex.db_down() is True                  # and reported


def test_no_store_is_db_down_not_silence():
    cortex.boot(None)
    _flushed(1.5)
    assert cortex.db_down() is True


def test_a_store_that_comes_back_clears_the_fault():
    class Flaky(store.MemoryStore):
        def __init__(self):
            super().__init__()
            self.fail = True

        def save_settings(self, values):
            if self.fail:
                raise store.StoreUnreachable("down")
            super().save_settings(values)

    s = Flaky()
    cortex.boot(s)
    cortex.set_state({"mood": "hurt"})
    _flushed()
    assert cortex.db_down() is True
    s.fail = False
    _flushed(2.5)
    assert cortex.db_down() is False
    assert s.settings["mood"] == "hurt"      # held, then written, not lost


# -- counters flush on their own timer ------------------------------------- #

def test_a_counter_is_never_written_to_the_store():
    """Counters count THIS boot — motion.py says so — so restoring one would
    claim a count the body is not counting. They ride the feed and stay out."""
    s = store.MemoryStore()
    cortex.boot(s)
    for i in range(50):
        cortex.set_state({"snr_count": i})
    _flushed(1.5)
    assert s.settings == {}                # not one write, not fifty
    assert cortex.get_state()["snr_count"] == 49


# -- slave class: records -------------------------------------------------- #

def test_records_are_queued_and_written():
    s = store.MemoryStore()
    cortex.boot(s)
    cortex.log_event("mood", {"feeling": "pleased"}, ts=1234.5)
    _flushed()
    assert s.events == [{"ts": 1234.5, "kind": "mood",
                         "detail": {"feeling": "pleased"}}]


def test_records_survive_a_store_that_is_down():
    """A blip costs a delay, not a hole in the history."""
    class Flaky(store.MemoryStore):
        def __init__(self):
            super().__init__()
            self.fail = True

        def add_event(self, ts, kind, detail=None):
            if self.fail:
                raise store.StoreUnreachable("down")
            super().add_event(ts, kind, detail)

    s = Flaky()
    cortex.boot(s)
    cortex.log_event("fault", {"code": "OLED LOOP"}, ts=1.0)
    _flushed()
    assert s.events == []
    s.fail = False
    _flushed(2.5)
    assert s.events and s.events[0]["kind"] == "fault"


def test_a_record_keeps_the_time_it_happened():
    """Not the time it landed. A record with the wrong timestamp is a lie about
    when something broke."""
    s = store.MemoryStore()
    cortex.boot(s)
    cortex.log_event("boot", {"svc": "cortex"}, ts=99.0)
    _flushed()
    assert s.events[0]["ts"] == 99.0


def test_record_queue_is_bounded():
    """An outage must not eat the body's RAM."""
    cortex.boot(None)
    for i in range(store.PENDING_RECORDS_MAX + 10):
        cortex.log_event("tick", {"i": i})
    assert len(cortex._pending_records) <= store.PENDING_RECORDS_MAX


# -- the face's orientation is a setting, not a per-frame transform -------- #

def test_the_orientation_is_persisted():
    """A face mounted upside down must still be upside down after a reboot, so
    this is a SETTING: it goes to the store like any other."""
    assert "oled_flip" in store.SETTING_KEYS
    assert "oled_flip" in store.PERSISTED


def test_the_orientation_defaults_to_the_face_as_wired():
    """0xA1/0xC8 is how the panel is wired today. The default must be the face
    Divv already knows — a flip that changes on first boot is a bug."""
    cortex.boot(None)
    assert cortex.get_state()["oled_flip"] is True


def test_the_orientation_survives_a_round_trip():
    s = store.MemoryStore()
    cortex.boot(s)
    cortex.set_state({"oled_flip": False})
    _flushed()
    assert s.settings["oled_flip"] is False
    cortex.boot(s)                      # as if the body had restarted
    assert cortex.get_state()["oled_flip"] is False


def test_the_panel_is_told_the_orientation_on_the_panel():
    """Two commands to the SH1106, not a rotated pixel: the bench display
    records what it was told."""
    from loa.oled import driver
    d = driver.NullDisplay()
    assert getattr(d, "_flip", None) is None
    d.set_flip(False)
    assert d._flip is False
    d.set_flip(True)
    assert d._flip is True


# -- the trend is computed, not stored as a verdict ------------------------ #

def test_trend_is_steady_without_enough_samples():
    out = cortex._trend_from_samples([], time.time(), 3600)
    assert out["dir"] == "steady"


def test_trend_reads_rising_from_synthetic_samples():
    now = time.time()
    pairs = [(now - 1800 + i * 60, 1010.0 + i * 0.5) for i in range(30)]
    out = cortex._trend_from_samples(pairs, now, 3600)
    assert out["dir"] == "rising"
    assert out["slope_hpa_per_h"] > 0
