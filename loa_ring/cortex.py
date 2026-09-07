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
            updated_at REAL NOT NULL
        )""")
        _conn.execute("""CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            kind TEXT NOT NULL,
            detail TEXT
        )""")
        _conn.execute(
            "INSERT OR IGNORE INTO state (id, updated_at) VALUES (1, ?)",
            (time.time(),))
    return _conn


def _row_to_state(row):
    return {
        "ring_state": row[0],
        "pending_event": row[1],
        "mood": row[2],
        "expression": row[3],
        "oled_mode": row[4],
        "oled_text": row[5],
        "oled_dim": bool(row[6]),
        "updated_at": row[7],
    }


def get_state():
    with _lock:
        row = _connect().execute(
            "SELECT ring_state, pending_event, mood, expression, oled_mode, "
            "oled_text, oled_dim, updated_at FROM state WHERE id = 1"
        ).fetchone()
    if row is None:
        return _defaults()
    return _row_to_state(row)


def _defaults():
    return {
        "ring_state": "home", "pending_event": None, "mood": "calm",
        "expression": None, "oled_mode": "scope", "oled_text": None,
        "oled_dim": False, "updated_at": 0.0,
    }


def set_state(fields):
    """Update the state row. fields: any of ring_state/pending_event/mood/
    expression/oled_mode/oled_text/oled_dim. Autocommit per statement."""
    allowed = {"ring_state", "pending_event", "mood", "expression",
               "oled_mode", "oled_text", "oled_dim"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    values = [int(fields["oled_dim"]) if k == "oled_dim"
              else fields[k] for k in fields]
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