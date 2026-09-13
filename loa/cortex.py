"""cortex — the nervous system. The body's live state in RAM, and the store.

The state is RAM. Not a database row, not a file. The live picture of the body
is what the cortex is holding right now, and it goes out on the topics every
TICK_S. Anything a consumer wants to know about "now", it gets from the feed.

Until 2026-09-12 this module was a SQLite row (id=1) that every daemon polled
every frame. That is the thing the topics replaced: a local read that masquerades
as a remote one, answers a plausible empty question instead of failing, and
leaves the body's live state on an SD card that browns out. Divv's posture, on
file: live state lives in RAM, only config and records persist.

WHAT PERSISTS, and how (see loa/store.py):

  Settings and counters are MASTER — held in RAM, written to the store, and if
  the store cannot take them, held PENDING and reported as the `DB DOWN` fault.
  A setting that cannot be flushed will not survive a reboot, and that has to be
  visible rather than discovered later.

  Records are SLAVE — queued and written best-effort, never blocking the tick.
  A body whose picture stops because a store is busy is worse than a body with a
  hole in its history.

Counters flush on a timer, not on every increment: snr_count moves with every
sonar reading, and a database write per reading is a write per reading.
"""
import copy
import threading
import time

from . import store as store_mod

#: Live-only keys: never persisted, gone with the power. A brown-out must not
#: leave a body claiming a mood that ended when the power did.
DEFAULTS = {
    # -- presentation / settings (MASTER) --
    "mood": "calm", "mood_set_at": None, "page": "sensors",
    "expression": None,
    "ring_state": "home", "oled_mode": "scope", "oled_text": None,
    "oled_dim": False,
    # The face's orientation, as MOUNTED. 0xA1+0xC8 (flipped=True) is the canon
    # pair; the panel as mounted on the body reads upside down with it, so the
    # as-built default is the turned pair — verified on glass 2026-09-12.
    # oled_flip True = the face as MOUNTED (0xA1 segment remap + 0xC8 reversed
    # scan, which is how the panel is wired). The default must be the face Divv
    # already knows — a flip that changes on first boot is a bug, not a feature.
    "oled_flip": True, "ripperdoc": False, "snr_on": True,
    # -- counters (MASTER) --
    "pir_count": 0, "snr_count": 0, "temp_count": 0, "baro_count": 0,
    # -- live readings (RAM only) --
    "pir_high": False, "pir_on_ts": None, "pir_last_hold": 0.0,
    "pir_last_ts": None,
    "snr_cm": None, "snr_ts": None,
    "temp_c": None, "hum_pct": None, "temp_ts": None,
    "pressure_hpa": None, "baro_temp_c": None, "baro_ts": None,
    "condition": "well",
    #: When the fault sweep was last HEARD. Not the same as `condition`: a body
    #: that has never been swept has no condition at all, and reading that
    #: silence as "well" is what let a body sit hurting with a calm face.
    "fault_ts": None,
    "baro_trend": "steady", "baro_series": [],
    "faults": [], "power": {}, "frag": {},
    "updated_at": 0.0,
}

#: Which state keys the store is allowed to persist. Settings and counters.
PERSISTED = set(store_mod.PERSISTED)

#: Counters are flushed at most this often. A counter that moves with every
#: reading must not become a database write with every reading.
#: The baro trend is read back from the store. Recompute at most this often:
#: it is a 3-hour window and it is not news every 500ms.
TREND_REFRESH_S = 30.0
TREND_WINDOW_S = 3 * 3600
TREND_DEADBAND_HPA_H = 0.3

_lock = threading.RLock()
_state = dict(DEFAULTS)
_store = None
_db_down = False
_pending = {}          # master-class changes the store has not taken yet
_pending_records = []  # (ts, kind, detail) waiting for the store
_last_trend = 0.0

_PUBLISHERS = []


# --------------------------------------------------------------------------- #
# the store
# --------------------------------------------------------------------------- #

def set_store(s):
    """Install the store. Tests pass a MemoryStore; the body gets aleph."""
    global _store
    with _lock:
        _store = s


def get_store():
    return _store


def db_down() -> bool:
    """True when a master-class change could not be written."""
    return _db_down


def store_ready() -> bool:
    """A store exists and has taken everything we have."""
    return _store is not None and not _db_down


def boot(store=None):
    """Load the master class and start the flush thread.

    Seeding from the store is the ONLY read of the store that a live decision
    hangs off: at boot the RAM is empty, and whatever is in the store becomes
    the truth by default. Everywhere else, reads of the store are history reads.
    """
    if store is not None:
        set_store(store)
    with _lock:
        s = _store
        if s is not None:
            try:
                for k, v in (s.load_settings() or {}).items():
                    if k in PERSISTED:
                        _state[k] = (v if k not in ("oled_dim", "ripperdoc",
                                                    "snr_on", "oled_flip")
                                     else bool(v))
                _db_down = False
            except Exception as e:                              # noqa: BLE001
                _db_down = True
                _complain("boot: could not read settings", e)
    threading.Thread(target=_flusher, daemon=True).start()
    return get_state()


def _complain(what, exc):
    """Loud, on stderr (the journal on the body). A silent store is a body that
    cannot remember, and it must not be discovered at the next reboot."""
    import sys
    print("loa-cortex: %s: %s: %s" % (what, type(exc).__name__, exc),
          file=sys.stderr, flush=True)


def _flusher():
    """Try to empty the pending master-class changes and the record queue.

    Settings flush as soon as they are pending. Records are drained here too, so
    a store blip costs a delay rather than a hole. Counters are not here at all:
    they count this boot and live on the feed, never in the store.
    """
    global _db_down
    while True:
        time.sleep(1.0)
        with _lock:
            s = _store
            pending = dict(_pending)
            records, _pending_records[:] = list(_pending_records), []
        if s is None:
            with _lock:
                _db_down = True
                _pending_records[:0] = records
            continue
        if not pending and not records:
            continue
        try:
            if pending:
                s.save_settings(pending)
            for ts, kind, detail in records:
                s.add_event(ts, kind, detail)
        except Exception as e:                                  # noqa: BLE001
            with _lock:
                if _db_down is False:
                    _complain("store not written — held pending", e)
                _db_down = True
                _pending_records[:0] = records
            continue
        with _lock:
            for key in pending:
                _pending.pop(key, None)
            _db_down = False


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #

def get_state():
    """A copy — callers must not be able to mutate the body by holding it."""
    with _lock:
        st = copy.deepcopy(_state)
        st["db_down"] = _db_down
        return st


def set_state(fields):
    """Merge fields into the live state and report the change.

    A field in the master class is marked pending and flushed by the background
    thread. A change is adopted IMMEDIATELY either way: the body keeps running
    while the store is unreachable, and the fault says so.
    """
    global _counters_dirty
    fields = {k: v for k, v in (fields or {}).items() if k in DEFAULTS}
    if not fields:
        return get_state()
    with _lock:
        _state.update(fields)
        _state["updated_at"] = time.time()
        for key in fields:
            if key in PERSISTED:
                # Only the master class is queued. A counter is live state: it
                # counts this boot and rides the feed, and the store never hears
                # about it.
                _pending[key] = _state[key]
    _report("state", get_state())
    return get_state()


def log_event(kind, detail=None, ts=None):
    """Queue a record.

    ts is accepted so a message that travelled — published, queued, consumed —
    is recorded at the time it HAPPENED rather than the time it landed. A record
    with the wrong timestamp is a lie about when something broke, and the
    timestamp is the only thing that makes a record usable.
    """
    when = time.time() if ts is None else ts
    with _lock:
        _pending_records.append((when, kind, detail))
        if len(_pending_records) > store_mod.PENDING_RECORDS_MAX:
            del _pending_records[:len(_pending_records)
                                - store_mod.PENDING_RECORDS_MAX]
    _report("event", {"ts": when, "kind": kind, "detail": detail})
    return when


def clear_event():
    """No-op kept for the old callers: the pending-event flag lived in the state
    row that no longer exists."""
    return None


# --------------------------------------------------------------------------- #
# publish hooks
# --------------------------------------------------------------------------- #
# The state module stays free of the wire. It reports what changed; the service
# decides what to do about it. This is what lets loa-cortex be the single
# publisher without cortex.py knowing ZMQ exists.

def on_publish(callback):
    """Register callback(kind, payload) for state changes and events."""
    _PUBLISHERS.append(callback)

    def off():
        try:
            _PUBLISHERS.remove(callback)
        except ValueError:
            pass
    return off


def _report(kind, payload):
    for cb in list(_PUBLISHERS):
        try:
            cb(kind, payload)
        except Exception:                                       # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# baro telemetry
# --------------------------------------------------------------------------- #
# Pressure is a stream, not a state. The store keeps the samples; the trend is
# computed from them and cached, because the window is three hours and the
# sparkline is not news every 500ms.

def _trend_from_samples(pairs, now, window_s):
    """Least-squares slope over the window -> trend dict. pairs: [(ts, hPa)].

    Pure — tests feed synthetic series without a store."""
    pts = [(t, p) for t, p in pairs if t >= now - window_s]
    if len(pts) < 2:
        return {"dir": "steady", "slope_hpa_per_h": 0.0, "delta_hpa": 0.0}
    n = len(pts)
    sx = sum(t for t, _ in pts)
    sy = sum(p for _, p in pts)
    sxx = sum(t * t for t, _ in pts)
    sxy = sum(t * p for t, p in pts)
    denom = n * sxx - sx * sx
    slope_h = ((n * sxy - sx * sy) / denom * 3600.0) if denom else 0.0
    delta = pts[-1][1] - pts[0][1]
    if slope_h > TREND_DEADBAND_HPA_H:
        d = "rising"
    elif slope_h < -TREND_DEADBAND_HPA_H:
        d = "falling"
    else:
        d = "steady"
    return {"dir": d, "slope_hpa_per_h": round(slope_h, 2),
            "delta_hpa": round(delta, 2)}


def baro_sample(pressure_hpa, baro_temp_c, ts=None):
    """Record a sample. Best-effort: the reading is already in RAM."""
    global _last_trend
    now = ts if ts is not None else time.time()
    with _lock:
        s = _store
        _last_trend = 0.0            # a new sample makes the cached trend stale
    if s is not None:
        try:
            s.add_baro_sample(now, pressure_hpa, baro_temp_c)
        except Exception as e:                                  # noqa: BLE001
            _complain("baro sample not written", e)


def baro_samples(since=None, limit=None):
    with _lock:
        s = _store
    if s is None:
        return []
    try:
        return s.baro_samples(since=since, limit=limit)
    except Exception as e:                                      # noqa: BLE001
        _complain("baro samples unreadable", e)
        return []


def baro_trend(window_s=TREND_WINDOW_S, now=None):
    """The cached trend, refreshed from the store at most every TREND_REFRESH_S.
    Returns the trend DICT plus a short series for the sparkline."""
    global _last_trend
    now = now if now is not None else time.time()
    with _lock:
        fresh = now - _last_trend < TREND_REFRESH_S
        cached = _state.get("baro_trend"), list(_state.get("baro_series") or [])
    if fresh:
        return cached[0], cached[1]
    rows = baro_samples(since=now - window_s)
    trend = _trend_from_samples([(t, p) for t, p, _ in rows], now, window_s)
    series = [p for _, p, _ in rows][-60:]
    with _lock:
        _state["baro_trend"] = trend["dir"]
        _state["baro_series"] = series
        _last_trend = now
    return trend["dir"], series


def history(limit=20):
    with _lock:
        s = _store
    if s is None:
        return []
    try:
        return s.history(limit=limit)
    except Exception as e:                                      # noqa: BLE001
        _complain("history unreadable", e)
        return []


def reset_for_tests():
    """Back to a clean body: no store, default state, no pending anything."""
    global _state, _store, _db_down, _pending, _pending_records
    global _last_trend
    with _lock:
        _state = dict(DEFAULTS)
        _store = None
        _db_down = False
        _pending = {}
        _pending_records = []
        _last_trend = 0.0
        _PUBLISHERS.clear()
