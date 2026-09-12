"""store — the body's memory. Postgres on aleph, and only the cortex writes it.

Settled with Divv 2026-09-12. The store is POSTGRES ON ALEPH, PRIMARY — not a
backstop, not a backup. SQLite on the body is gone, along with the `state` table:
live values are the feed's job, and nothing reads a database row to find out what
the body is doing now.

The store is for three things and only these:

  SETTINGS   ring mode, OLED dim, the console flag, the page, the sonar flag.
             Must be true again after a reboot.
  RECORDS    what happened: faults, commands that landed, mode changes, sensor
             readings, logs. Append-only, one row per occurrence.
  A TREND    pressure over time. The one sampled table, and it stays slow —
             a baro sample every ~10s. It exists so an overnight pressure move
             is visible, and overnight outlives every process.

NOT in it: the live readings and the current-state row.

MASTER OR SLAVE, by class:

  Settings and counters are MASTER. They live in RAM at runtime and are read back
  from the store at boot. Adopt a change IMMEDIATELY, hold it PENDING in RAM, and
  flush when the store is reachable. Never fail a command because a remote
  database hiccuped. But a setting that cannot be flushed IS a fault — `DB DOWN`
  — because it means the setting will not survive a reboot. The fault is what
  makes it visible instead of silent.

  Records, history and the trend are SLAVE. Nothing running ever asks them a
  question, and no component decides anything by reading them.

The tick can't afford to wait; a setting can't afford to be lost.

WHY NOT WRITE FIRST. The obvious rule — write to the store, and only adopt the
change once it has taken it — makes every command fail the moment aleph hiccups.
Hold-and-flush keeps the body working and still surfaces the failure, as a fault
on the face rather than a silent revert at the next boot.

WHY A TUNNEL. Aleph's postgres publishes only to aleph's own localhost:5434, so
something has to forward it. The `porky` database does the same thing the same
way: a systemd ssh tunnel, one login role per store, and ALTER DEFAULT PRIVILEGES
for tables and sequences so a dropped-and-recreated table stays reachable.
"""
import json
import threading
import time

#: Settings: the MASTER class. Must survive a reboot.
SETTING_KEYS = (
    "mood", "mood_set_at", "ring_state", "oled_mode", "oled_text", "oled_dim",
    "ripperdoc", "page", "snr_on", "expression", "oled_flip",
)

#: Cumulative counters, and deliberately NOT persisted. They count THIS boot —
#: motion.py says it plainly: "the N counter is per-boot: a rebooted body starts
#: at zero". Restoring one across a reboot would claim a count the body is not
#: counting. They are live state, they ride the feed, and they stay out of the
#: store. Named here so the decision has one home.
COUNTER_KEYS = ("pir_count", "snr_count", "temp_count", "baro_count")

#: Everything the store persists for the master class.
PERSISTED = SETTING_KEYS

#: How many records to hold while the store is unreachable before dropping the
#: oldest. A blip must not lose history; an outage must not eat the body's RAM.
PENDING_RECORDS_MAX = 5000


class StoreUnreachable(RuntimeError):
    """The store could not be written. Held pending; reported as a fault."""


class MemoryStore:
    """An in-process store. Tests only — never selected on the body."""

    persistent = False

    def __init__(self):
        self.settings = {}
        self.events = []
        self.samples = []

    def load_settings(self):
        return dict(self.settings)

    def save_settings(self, values):
        self.settings.update(values)

    def add_event(self, ts, kind, detail=None):
        self.events.append({"ts": ts, "kind": kind, "detail": detail})

    def add_baro_sample(self, ts, pressure_hpa, baro_temp_c=None):
        self.samples.append((ts, pressure_hpa, baro_temp_c))

    def baro_samples(self, since=None, limit=None):
        rows = [r for r in self.samples if since is None or r[0] >= since]
        rows.sort(key=lambda r: r[0])
        return rows[-limit:] if limit else rows

    def history(self, limit=20):
        return list(reversed(self.events))[:limit]

    def close(self):
        pass


class PostgresStore:
    """Postgres on aleph. The only writer in the system is the cortex."""

    persistent = True

    SCHEMA = (
        """CREATE TABLE IF NOT EXISTS settings (
               key TEXT PRIMARY KEY,
               value JSONB NOT NULL,
               updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
           )""",
        """CREATE TABLE IF NOT EXISTS events (
               id BIGSERIAL PRIMARY KEY,
               ts DOUBLE PRECISION NOT NULL,
               kind TEXT NOT NULL,
               detail JSONB
           )""",
        """CREATE TABLE IF NOT EXISTS baro_samples (
               id BIGSERIAL PRIMARY KEY,
               ts DOUBLE PRECISION NOT NULL,
               pressure_hpa DOUBLE PRECISION NOT NULL,
               baro_temp_c DOUBLE PRECISION
           )""",
        "CREATE INDEX IF NOT EXISTS idx_baro_samples_ts ON baro_samples (ts)",
    )

    def __init__(self, dsn, schema=True):
        self.dsn = dsn
        self._lock = threading.Lock()
        self._conn = None
        if schema:
            self._connect()

    def _connect(self):
        """A connection, made on demand. psycopg is imported here so a body
        without the driver still imports this module."""
        if self._conn is not None and not self._conn.closed:
            return self._conn
        import psycopg                               # noqa: PLC0415 (lazy)
        self._conn = psycopg.connect(self.dsn, autocommit=True,
                                     connect_timeout=5)
        for stmt in self.SCHEMA:
            self._conn.execute(stmt)
        return self._conn

    def _run(self, sql, params=()):
        with self._lock:
            try:
                cur = self._connect().execute(sql, params)
                return cur.fetchall() if cur.description else []
            except Exception:
                # Drop the connection so the next attempt reconnects rather
                # than reusing a socket that is already dead.
                try:
                    self._conn.close()
                except Exception:                               # noqa: BLE001
                    pass
                self._conn = None
                raise

    def load_settings(self):
        rows = self._run("SELECT key, value FROM settings")
        return {k: v for k, v in rows if k in PERSISTED}

    def save_settings(self, values):
        values = {k: v for k, v in values.items() if k in PERSISTED}
        if not values:
            return
        with self._lock:
            conn = self._connect()
            for key, value in values.items():
                conn.execute(
                    "INSERT INTO settings (key, value, updated_at) "
                    "VALUES (%s, %s, now()) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
                    "updated_at = now()", (key, json.dumps(value)))

    def add_event(self, ts, kind, detail=None):
        self._run("INSERT INTO events (ts, kind, detail) VALUES (%s, %s, %s)",
                  (float(ts), kind, json.dumps(detail) if detail is not None
                   else None))

    def add_baro_sample(self, ts, pressure_hpa, baro_temp_c=None):
        self._run("INSERT INTO baro_samples (ts, pressure_hpa, baro_temp_c) "
                  "VALUES (%s, %s, %s)", (float(ts), float(pressure_hpa),
                                          baro_temp_c))

    def baro_samples(self, since=None, limit=None):
        if since is None:
            rows = self._run("SELECT ts, pressure_hpa, baro_temp_c "
                             "FROM baro_samples ORDER BY ts")
        else:
            rows = self._run("SELECT ts, pressure_hpa, baro_temp_c "
                             "FROM baro_samples WHERE ts >= %s ORDER BY ts",
                             (float(since),))
        return rows[-limit:] if limit else rows

    def history(self, limit=20):
        rows = self._run("SELECT ts, kind, detail FROM events "
                         "ORDER BY id DESC LIMIT %s", (int(limit),))
        return [{"ts": ts, "kind": kind, "detail": detail}
                for ts, kind, detail in rows]

    def close(self):
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:                                       # noqa: BLE001
            pass


def from_config(cfg=None) -> "PostgresStore | None":
    """The configured store, or None when there is no DSN.

    None is not a silent fallback: the cortex reports `DB DOWN` and holds
    everything pending, which is exactly what an unreachable aleph looks like.
    """
    import os
    from . import config
    cfg = cfg if cfg is not None else config.load()
    dsn = os.environ.get("LOA_PG_DSN") or cfg.get("pg_dsn")
    if not dsn:
        return None
    return PostgresStore(dsn)
