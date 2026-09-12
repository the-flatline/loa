"""cortex — the nervous system. SQLite state + event log for the loa body.

One state row (id=1) holds everything the daemons need: what the ring
should do, what the face should show, what mood the brain set. An append-
only events table is the body's memory — every mood, expression, ring
command and daemon boot, timestamped. The brain interrogates /state?history=N
to know what it has been projecting.

The daemons poll the state row every frame (cheap local reads); the API
writes it. Same decoupled shape as the old flag files, except now there is
history, there are parameters, and nothing gets stuck when a daemon dies.

Path: ~/.loa/cortex.db by default, overridable via loa.conf cortex_db.
"""

import json
import os
import sqlite3
import threading
import time

from . import config

DB_PATH = os.path.expanduser("~/.loa/cortex.db")

_lock = threading.Lock()
_conn = None


def _connect():
    global _conn
    if _conn is None:
        path = os.path.expanduser(config.load().get("cortex_db", DB_PATH))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _conn = sqlite3.connect(path, check_same_thread=False,
                                isolation_level=None)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("""CREATE TABLE IF NOT EXISTS state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            ring_state TEXT NOT NULL DEFAULT 'home',
            pending_event TEXT,
            mood TEXT NOT NULL DEFAULT 'calm',
            expression TEXT,
            oled_mode TEXT NOT NULL DEFAULT 'scope',
            oled_text TEXT,
            oled_dim INTEGER NOT NULL DEFAULT 0,
            ripperdoc INTEGER NOT NULL DEFAULT 0,
            pir_high INTEGER NOT NULL DEFAULT 0,
            sense_ts REAL,
            sense_count INTEGER NOT NULL DEFAULT 0,
            pir_on_ts REAL,
            pir_last_hold REAL NOT NULL DEFAULT 0,
            ripperdoc_page TEXT NOT NULL DEFAULT 'sensors',
            snr_cm REAL,
            snr_ts REAL,
            snr_count INTEGER NOT NULL DEFAULT 0,
            temp_c REAL,
            hum_pct REAL,
            temp_ts REAL,
            temp_count INTEGER NOT NULL DEFAULT 0,
            pressure_hpa REAL,
            baro_temp_c REAL,
            baro_ts REAL,
            baro_count INTEGER NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL
        )""")
        _conn.execute("""CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            kind TEXT NOT NULL,
            detail TEXT
        )""")
        _conn.execute("""CREATE TABLE IF NOT EXISTS baro_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            pressure_hpa REAL NOT NULL,
            baro_temp_c REAL
        )""")
        _conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_baro_samples_ts "
            "ON baro_samples (ts)")
        _conn.execute(
            "INSERT OR IGNORE INTO state (id, updated_at) VALUES (1, ?)",
            (time.time(),))
        _ensure_schema()
    return _conn


def _ensure_schema():
    """Migrate older state rows: add sense/ripperdoc columns if missing."""
    cols = {r[1] for r in _conn.execute("PRAGMA table_info(state)").fetchall()}
    for name, ddl in (
        ("ripperdoc", "INTEGER NOT NULL DEFAULT 0"),
        ("pir_high", "INTEGER NOT NULL DEFAULT 0"),
        ("sense_ts", "REAL"),
        ("sense_count", "INTEGER NOT NULL DEFAULT 0"),
        ("pir_on_ts", "REAL"),
        ("pir_last_hold", "REAL NOT NULL DEFAULT 0"),
        ("ripperdoc_page", "TEXT NOT NULL DEFAULT 'sensors'"),
        ("snr_cm", "REAL"),
        ("snr_ts", "REAL"),
        ("snr_count", "INTEGER NOT NULL DEFAULT 0"),
        ("temp_c", "REAL"),
        ("hum_pct", "REAL"),
        ("temp_ts", "REAL"),
        ("temp_count", "INTEGER NOT NULL DEFAULT 0"),
        ("pressure_hpa", "REAL"),
        ("baro_temp_c", "REAL"),
        ("baro_ts", "REAL"),
        ("baro_count", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if name not in cols:
            _conn.execute(f"ALTER TABLE state ADD COLUMN {name} {ddl}")


def _row_to_state(row):
    return {
        "ring_state": row[0],
        "pending_event": row[1],
        "mood": row[2],
        "expression": row[3],
        "oled_mode": row[4],
        "oled_text": row[5],
        "oled_dim": bool(row[6]),
        "ripperdoc": bool(row[7]),
        "pir_high": bool(row[8]),
        "sense_ts": row[9],
        "sense_count": row[10],
        "pir_on_ts": row[11],
        "pir_last_hold": row[12],
        "ripperdoc_page": row[13],
        "snr_cm": row[14],
        "snr_ts": row[15],
        "snr_count": row[16],
        "temp_c": row[17],
        "hum_pct": row[18],
        "temp_ts": row[19],
        "temp_count": row[20],
        "pressure_hpa": row[21],
        "baro_temp_c": row[22],
        "baro_ts": row[23],
        "baro_count": row[24],
        "updated_at": row[25],
    }


def get_state():
    with _lock:
        row = _connect().execute(
            "SELECT ring_state, pending_event, mood, expression, oled_mode, "
            "oled_text, oled_dim, ripperdoc, pir_high, sense_ts, sense_count, "
            "pir_on_ts, pir_last_hold, ripperdoc_page, snr_cm, snr_ts, "
            "snr_count, temp_c, hum_pct, temp_ts, temp_count, "
            "pressure_hpa, baro_temp_c, baro_ts, baro_count, updated_at "
            "FROM state WHERE id = 1"
        ).fetchone()
    if row is None:
        return _defaults()
    return _row_to_state(row)


def _defaults():
    return {
        "ring_state": "home", "pending_event": None, "mood": "calm",
        "expression": None, "oled_mode": "scope", "oled_text": None,
        "oled_dim": False, "ripperdoc": False, "pir_high": False,
        "sense_ts": None, "sense_count": 0, "pir_on_ts": None,
        "pir_last_hold": 0.0, "ripperdoc_page": "sensors",
        "snr_cm": None, "snr_ts": None, "snr_count": 0,
        "temp_c": None, "hum_pct": None, "temp_ts": None, "temp_count": 0,
        "pressure_hpa": None, "baro_temp_c": None, "baro_ts": None,
        "baro_count": 0,
        "updated_at": 0.0,
    }


def set_state(fields):
    """Update the state row. fields: any of ring_state/pending_event/mood/
    expression/oled_mode/oled_text/oled_dim. Autocommit per statement."""
    allowed = {"ring_state", "pending_event", "mood", "expression",
               "oled_mode", "oled_text", "oled_dim", "ripperdoc",
               "pir_high", "sense_ts", "sense_count", "ripperdoc_page",
               "pir_on_ts", "pir_last_hold", "snr_cm", "snr_ts", "snr_count",
               "temp_c", "hum_pct", "temp_ts", "temp_count",
               "pressure_hpa", "baro_temp_c", "baro_ts", "baro_count"}
    int_fields = {"oled_dim", "ripperdoc", "pir_high", "sense_count",
                  "snr_count", "temp_count", "baro_count"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    values = [int(fields[k]) if k in int_fields else fields[k]
              for k in fields]
    with _lock:
        _connect().execute(
            f"UPDATE state SET {sets}, updated_at = ? WHERE id = 1",
            (*values, time.time()))


def clear_event():
    set_state({"pending_event": None})


def log_event(kind, detail=None):
    with _lock:
        _connect().execute(
            "INSERT INTO events (ts, kind, detail) VALUES (?, ?, ?)",
            (time.time(), kind, json.dumps(detail) if detail is not None
             else None))


# -- baro telemetry -------------------------------------------------------
#
# Pressure is a stream, not a state: the state row holds the latest read,
# baro_samples keeps the history the trend is computed from. 10s cadence is
# ~8.6k rows/day; samples older than 7 days are pruned on insert.

BARO_KEEP_S = 7 * 86400


def baro_sample(pressure_hpa, baro_temp_c, ts=None):
    now = ts if ts is not None else time.time()
    with _lock:
        _connect().execute(
            "INSERT INTO baro_samples (ts, pressure_hpa, baro_temp_c) "
            "VALUES (?, ?, ?)", (now, pressure_hpa, baro_temp_c))
        _connect().execute(
            "DELETE FROM baro_samples WHERE ts < ?", (now - BARO_KEEP_S,))


def baro_samples(since=None, limit=None):
    """Rows (ts, pressure_hpa, baro_temp_c) ascending, optional window."""
    with _lock:
        if since is not None:
            rows = _connect().execute(
                "SELECT ts, pressure_hpa, baro_temp_c FROM baro_samples "
                "WHERE ts >= ? ORDER BY ts", (since,)).fetchall()
        else:
            rows = _connect().execute(
                "SELECT ts, pressure_hpa, baro_temp_c FROM baro_samples "
                "ORDER BY ts").fetchall()
    if limit is not None:
        rows = rows[-limit:]
    return rows


TREND_DEADBAND_HPA_H = 0.3    # |slope| below this reads as steady


def _trend_from_samples(pairs, now, window_s):
    """Least-squares slope over the window -> trend dict. pairs: [(ts, hPa)].
    Pure — tests feed synthetic series without a database."""
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


def baro_trend(window_s=3 * 3600, now=None):
    now = now if now is not None else time.time()
    rows = baro_samples(since=now - window_s)
    return _trend_from_samples([(t, p) for t, p, _ in rows], now, window_s)


def history(limit=20):
    with _lock:
        rows = _connect().execute(
            "SELECT ts, kind, detail FROM events ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()
    out = []
    for ts, kind, detail in reversed(rows):
        out.append({
            "ts": ts,
            "kind": kind,
            "detail": json.loads(detail) if detail else None,
        })
    return out