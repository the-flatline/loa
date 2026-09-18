//! The store: Postgres on aleph, reached through a loopback tunnel. The
//! cortex is the ONLY writer in the system — a second process writing
//! records would be a second opinion about what happened.
//!
//! One connection, made lazily, dropped and remade on any error: a socket
//! that died mid-tunnel must not be reused by the next flush. The store
//! thread owns it exclusively; everything else is a `spawn_blocking` away.
//!
//! Failure is visible: an unreachable store raises `db_down` into the fault
//! rows as `DB DOWN`, so the body reports its own memory loss instead of
//! quietly forgetting. Records are capped in RAM while the store is down
//! (oldest dropped first) so a long outage cannot eat the Pi.
//!
//! No async in here. Blocking is the right shape for a database the cortex
//! visits from one thread at its own pace.

use std::str::FromStr;

use serde_json::Value;

/// The settings that survive a reboot. The two dead keys the reference
/// carries (`mood_set_at`, `expression`) are not listed: writing a key
/// nobody reads is a tombstone with no body.
pub const PERSISTED: [&str; 9] = [
    "mood",
    "page",
    "ring_state",
    "oled_mode",
    "oled_text",
    "oled_dim",
    "ripperdoc",
    "snr_on",
    "oled_flip",
];

/// The door's boot truth: bools come back as JSON bools from the old writer
/// and as 0/1 from anything newer. Coerce on read, never on write.
pub const BOOL_KEYS: [&str; 4] = ["oled_dim", "ripperdoc", "snr_on", "oled_flip"];

/// Unflushed records while the store is unreachable. Oldest drop first: the
/// newest truth is the one worth keeping.
pub const PENDING_RECORDS_MAX: usize = 5000;

pub struct Store {
    dsn: String,
    conn: Option<postgres::Client>,
}

impl Store {
    pub fn new(dsn: String) -> Self {
        Self { dsn, conn: None }
    }

    /// A live connection, schema ensured. Errors travel; the caller decides
    /// how loudly to complain — and complains by fault row, not by crash.
    fn connect(&mut self) -> Result<&mut postgres::Client, String> {
        if self.conn.is_some() {
            return Ok(self.conn.as_mut().unwrap());
        }
        let cfg = postgres::Config::from_str(&self.dsn)
            .map_err(|e| format!("bad DSN: {e}"))?;
        let mut client = cfg
            .connect(postgres::NoTls)
            .map_err(|e| format!("connect: {e}"))?;
        for stmt in SCHEMA {
            client
                .batch_execute(stmt)
                .map_err(|e| format!("schema: {e}"))?;
        }
        self.conn = Some(client);
        Ok(self.conn.as_mut().unwrap())
    }

    /// Run one statement; on any error, drop the connection so the next call
    /// reconnects instead of writing into a dead socket.
    fn with_conn<T>(
        &mut self,
        f: impl FnOnce(&mut postgres::Client) -> Result<T, postgres::Error>,
    ) -> Result<T, String> {
        let client = self.connect()?;
        match f(client) {
            Ok(v) => Ok(v),
            Err(e) => {
                self.conn = None;
                Err(format!("store: {e}"))
            }
        }
    }

    pub fn load_settings(&mut self) -> Result<Vec<(String, Value)>, String> {
        self.with_conn(|c| {
            let rows = c.query("SELECT key, value FROM settings", &[])?;
            Ok(rows
                .iter()
                .filter_map(|r| {
                    let k: String = r.get(0);
                    let postgres::types::Json(v): postgres::types::Json<Value> = r.get(1);
                    PERSISTED.contains(&k.as_str()).then(|| (k, v))
                })
                .collect())
        })
    }

    /// Upsert the master-class keys. Values are JSON as the state holds them
    /// — a bool, a string, a number — and ride in as the crate's Json type,
    /// which is the JSONB the column actually wants. A String parameter is
    /// TEXT to Postgres and TEXT does not become JSONB silently.
    pub fn save_settings(&mut self, values: &[(String, Value)]) -> Result<(), String> {
        if values.is_empty() {
            return Ok(());
        }
        self.with_conn(|c| {
            for (k, v) in values {
                if !PERSISTED.contains(&k.as_str()) {
                    continue; // a key outside the contract is a bug upstream
                }
                c.execute(
                    "INSERT INTO settings (key, value, updated_at) \
                     VALUES ($1, $2, now()) \
                     ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, \
                     updated_at = now()",
                    &[k, &postgres::types::Json(v)],
                )?;
            }
            Ok(())
        })
    }

    pub fn add_event(&mut self, ts: f64, kind: &str, detail: Option<Value>) -> Result<(), String> {
        self.with_conn(|c| {
            c.execute(
                "INSERT INTO events (ts, kind, detail) VALUES ($1, $2, $3)",
                &[&ts, &kind, &detail.map(postgres::types::Json)],
            )?;
            Ok(())
        })
    }

    pub fn add_baro_sample(&mut self, ts: f64, hpa: f64) -> Result<(), String> {
        self.with_conn(|c| {
            c.execute(
                "INSERT INTO baro_samples (ts, pressure_hpa) VALUES ($1, $2)",
                &[&ts, &hpa],
            )?;
            Ok(())
        })
    }

    /// The window's samples, oldest first — the durable half of the trend
    /// story, read only when the sparkline wants history the RAM window
    /// aged out of.
    pub fn baro_samples_since(&mut self, since: f64) -> Result<Vec<(f64, f64)>, String> {
        self.with_conn(|c| {
            let rows = c.query(
                "SELECT ts, pressure_hpa FROM baro_samples \
                 WHERE ts >= $1 ORDER BY ts",
                &[&since],
            )?;
            Ok(rows.iter().map(|r| (r.get(0), r.get(1))).collect())
        })
    }
}

/// The same three tables the Python created IF NOT EXISTS, so either body
/// boots against the other's database and nobody rewrites history.
const SCHEMA: [&str; 4] = [
    "CREATE TABLE IF NOT EXISTS settings (\
       key TEXT PRIMARY KEY, \
       value JSONB NOT NULL, \
       updated_at TIMESTAMPTZ NOT NULL DEFAULT now())",
    "CREATE TABLE IF NOT EXISTS events (\
       id BIGSERIAL PRIMARY KEY, \
       ts DOUBLE PRECISION NOT NULL, \
       kind TEXT NOT NULL, \
       detail JSONB)",
    "CREATE TABLE IF NOT EXISTS baro_samples (\
       id BIGSERIAL PRIMARY KEY, \
       ts DOUBLE PRECISION NOT NULL, \
       pressure_hpa DOUBLE PRECISION NOT NULL, \
       baro_temp_c DOUBLE PRECISION)",
    "CREATE INDEX IF NOT EXISTS idx_baro_samples_ts ON baro_samples (ts)",
];
