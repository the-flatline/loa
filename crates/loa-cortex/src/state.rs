//! The body's state, and the rules for moving it.
//!
//! A flat, typed record — the same shape the Python reference holds, with its
//! known dead weight left behind: `mood_set_at` and `expression` exist in the
//! persisted key list for compatibility with settings already in the DB, but
//! nothing writes or reads them here, because nothing ever did.
//!
//! The merge rule is proto presence, not truthiness: a daemon that did not
//! measure a field sends no field, and `false`/`0` that WAS measured must
//! still land. prost models exactly that with Option, which is why this struct
//! can merge without a presence side-channel.

use std::collections::VecDeque;

/// One fault row, structured — a consumer that has to split a string to find
/// out whether something hurts is a consumer that will get it wrong.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FaultRow {
    pub level: String, // "fault" | "warn"
    pub code: String,  // "OLED LOOP", "DB DOWN"
    pub text: String,
    pub face: Option<String>, // the short word the glass has room for
}

#[derive(Debug, Clone)]
pub struct State {
    // ---- settings: persisted, adopted at boot, moved only by verbs ----
    pub mood: String,
    pub page: String,
    pub ring_state: String,
    pub oled_mode: String,
    pub oled_text: Option<String>,
    pub oled_dim: bool,
    /// Flipped is the default: the panel is mounted upside down in the shell.
    pub oled_flip: bool,
    pub ripperdoc: bool,
    pub snr_on: bool,
    /// Dead keys kept for round-tripping the settings table, per the rule
    /// that a port must not invent writers for them.
    pub mood_set_at: Option<f64>,
    pub expression: Option<String>,

    // ---- readings: written by ingest, never persisted ----
    pub pir_high: bool,
    pub pir_count: u32,
    pub pir_last_ts: Option<f64>,
    /// A duration, not an event: 0.0 is "no hold measured", which is why it
    /// is not an Option in the reference and is not one here.
    pub pir_last_hold: f64,
    pub pir_on_ts: Option<f64>,

    pub snr_cm: Option<f64>,
    pub snr_count: u32,
    pub snr_ts: Option<f64>,

    pub temp_c: Option<f64>,
    pub hum_pct: Option<f64>,
    pub temp_ts: Option<f64>,
    pub temp_count: u32,

    pub pressure_hpa: Option<f64>,
    pub baro_temp_c: Option<f64>,
    pub baro_ts: Option<f64>,
    pub baro_count: u32,

    // ---- faults: the sweep's latest report, held in RAM ----
    pub faults: Vec<FaultRow>,
    pub fault_ts: Option<f64>,
    pub condition: String,
}

/// The window the trend is computed over, the deadband, and the sparkline
/// length — the Python reference's own numbers (state.py:73-75), kept.
/// What changes in the rebuild: the samples live in a RAM window and the
/// trend is recomputed when a sample arrives, not on a 30 s refresh timer.
/// News in, news out; the DB keeps the durable record either way.
const BARO_WINDOW_S: f64 = 3.0 * 3600.0;
const BARO_SERIES_POINTS: usize = 60;
const BARO_DEADBAND_HPA_H: f64 = 0.3;

#[derive(Default)]
pub struct Cortex {
    pub st: State,
    baro: VecDeque<(f64, f64)>, // (ts, pressure_hpa), oldest first
    /// The last frame each device pushed up, as it pushed it. The relay
    /// stores these on its way past, and every consumer-facing publish of
    /// `ripperdoc` carries the face inside it — one shape, one source.
    pub last_face: Option<Vec<u8>>,
    pub last_ring: Option<Vec<u8>>,
}

impl Default for State {
    fn default() -> Self {
        Self {
            mood: "calm".into(),
            page: "sensors".into(),
            ring_state: "home".into(),
            oled_mode: "scope".into(),
            oled_text: None,
            oled_dim: false,
            oled_flip: true,
            ripperdoc: false,
            snr_on: true,
            mood_set_at: None,
            expression: None,
            pir_high: false,
            pir_count: 0,
            pir_last_ts: None,
            pir_last_hold: 0.0,
            pir_on_ts: None,
            snr_cm: None,
            snr_count: 0,
            snr_ts: None,
            temp_c: None,
            hum_pct: None,
            temp_ts: None,
            temp_count: 0,
            pressure_hpa: None,
            baro_temp_c: None,
            baro_ts: None,
            baro_count: 0,
            faults: Vec::new(),
            fault_ts: None,
            condition: loa_common::condition::WELL.into(),
        }
    }
}

impl Cortex {
    pub fn new() -> Self {
        Self::default()
    }

    /// Merge one decoded reading into state. Presence is the only signal:
    /// None skips, Some writes — even Some(false) and Some(0).
    pub fn merge(&mut self, msg: &loa_wire::Msg) {
        use loa_wire::pb::envelope::Body as B;
        match msg.env.body.as_ref() {
            Some(B::Pir(p)) => {
                if let Some(v) = p.high {
                    self.st.pir_high = v;
                }
                // The count belongs to the daemon that counted the pulses.
                // Absence is absence: no invented last-resort increment.
                if let Some(v) = p.count {
                    self.st.pir_count = v;
                }
                if let Some(v) = p.last_ts {
                    self.st.pir_last_ts = Some(v);
                }
                // Both `ts` and `last_ts` map to pir_last_ts, as in the
                // reference — duplicate mapping, duplicate truth.
                if let Some(v) = p.ts {
                    self.st.pir_last_ts = Some(v);
                }
                if let Some(v) = p.last_hold {
                    self.st.pir_last_hold = v;
                }
                if let Some(v) = p.on_ts {
                    self.st.pir_on_ts = Some(v);
                }
            }
            Some(B::Sonar(s)) => {
                if let Some(v) = s.cm {
                    self.st.snr_cm = Some(v);
                }
                if let Some(v) = s.count {
                    self.st.snr_count = v;
                }
                if let Some(v) = s.ts {
                    self.st.snr_ts = Some(v);
                }
            }
            Some(B::Weather(w)) => {
                if let Some(v) = w.temp_c {
                    self.st.temp_c = Some(v);
                }
                if let Some(v) = w.hum_pct {
                    self.st.hum_pct = Some(v);
                }
                if let Some(v) = w.ts {
                    self.st.temp_ts = Some(v);
                }
                if let Some(v) = w.count {
                    self.st.temp_count = v;
                }
                if let Some(v) = w.pressure_hpa {
                    self.st.pressure_hpa = Some(v);
                    self.push_baro(w.ts.unwrap_or(0.0), v);
                    // the air arrived with pressure in it: the baro fields
                    // mean "the last pressure reading", so they ride along
                    if let Some(t) = w.ts {
                        self.st.baro_ts = Some(t);
                    }
                    if let Some(c) = w.count {
                        self.st.baro_count = c;
                    }
                }
                // `trend` and `series` on an inbound Weather are ignored:
                // the trend is DERIVED from the cortex's own sample window.
                // A daemon that measured a chip has no business computing
                // what three measurements mean.
            }
            Some(B::Fault(f)) => {
                self.st.faults = f
                    .rows
                    .iter()
                    .map(|r| FaultRow {
                        level: r.level.clone(),
                        code: r.code.clone(),
                        text: r.text.clone(),
                        face: r.face.clone(),
                    })
                    .collect();
                self.st.fault_ts = Some(f.ts.unwrap_or_else(now_s));
                // The sweep's condition rides the wire when the sweep has an
                // opinion, and that opinion can be `mute` — its own stale
                // report is exactly the case where rows are empty and no
                // recompute could ever say mute. Presence is the only merge.
                if let Some(c) = &f.condition {
                    self.st.condition = c.clone();
                }
            }
            // Ripperdoc/Ring arriving inbound is the frame path; they ride a
            // separate PULL leg through the relay, never the state merge. A
            // Power or Event message carries nothing to merge into state:
            // rails are a read-on-publish number, events are records.
            _ => {}
        }
    }

    /// Record a pressure sample into the trend window. Called from merge the
    /// instant a reading carrying pressure arrives — news in, news out.
    pub fn push_baro(&mut self, ts: f64, hpa: f64) {
        self.baro.push_back((ts, hpa));
        while let Some(&(first, _)) = self.baro.front() {
            if ts - first > BARO_WINDOW_S {
                self.baro.pop_front();
            } else {
                break;
            }
        }
    }

    /// The trend, exactly as the reference computes it: least-squares slope
    /// over the window in hPa/hour, direction against the 0.3 deadband,
    /// sparkline the last 60 pressures oldest-first. slope and delta are
    /// reported numbers; only the slope decides direction. A window with
    /// fewer than two points is steady — not rising, not falling, boring.
    pub fn baro_trend(&self) -> (String, Vec<f64>) {
        let n = self.baro.len() as f64;
        if self.baro.len() < 2 {
            return ("steady".into(), vec![]);
        }
        let (sx, sy, sxx, sxy) = self.baro.iter().fold(
            (0.0, 0.0, 0.0, 0.0),
            |(sx, sy, sxx, sxy), &(t, p)| {
                (sx + t, sy + p, sxx + t * t, sxy + t * p)
            },
        );
        let denom = n * sxx - sx * sx;
        let slope_h = if denom != 0.0 {
            (n * sxy - sx * sy) / denom * 3600.0
        } else {
            0.0
        };
        let dir = if slope_h > BARO_DEADBAND_HPA_H {
            "rising"
        } else if slope_h < -BARO_DEADBAND_HPA_H {
            "falling"
        } else {
            "steady"
        };
        let series: Vec<f64> = self
            .baro
            .iter()
            .rev()
            .take(BARO_SERIES_POINTS)
            .rev()
            .map(|&(_, v)| v)
            .collect();
        (dir.into(), series)
    }

    /// The published `fault` topic's condition: fault⇒hurts, warn⇒niggle,
    /// else well. This function never emits mute — that is the renderer's
    /// reading of SILENCE, not a computation over rows.
    pub fn condition_from(rows: &[FaultRow]) -> String {
        if rows.iter().any(|r| r.level == "fault") {
            loa_common::condition::HURTS.into()
        } else if rows.iter().any(|r| r.level == "warn") {
            loa_common::condition::NIGGLE.into()
        } else {
            loa_common::condition::WELL.into()
        }
    }

    /// What the glass and ring draw: no sweep report has ever arrived, the
    /// body is MUTE — silence is not health. A never-swept body therefore
    /// publishes condition="well" on the feed while its own displays draw
    /// mute. That divergence is the design; reproduce it, never "fix" it.
    pub fn renderer_condition(st: &State) -> String {
        if st.fault_ts.is_none() {
            loa_common::condition::MUTE.into()
        } else {
            st.condition.clone()
        }
    }

    /// The DB DOWN row, injected when the store cannot take a flush. A fault
    /// the DB itself would have recorded, raised by the only writer instead.
    pub fn injected_faults(db_down: bool) -> Vec<FaultRow> {
        if db_down {
            vec![FaultRow {
                level: "fault".into(),
                code: "DB DOWN".into(),
                text: "settings cannot be flushed to the store".into(),
                face: Some("STORE".into()),
            }]
        } else {
            Vec::new()
        }
    }
}

pub fn now_s() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use loa_wire::{envelope, pb};

    fn msg(body: pb::envelope::Body) -> loa_wire::Msg {
        loa_wire::Msg {
            topic: "pir".into(),
            env: envelope(body),
        }
    }

    #[test]
    fn absence_skips_and_presence_lands_even_false() {
        let mut cx = Cortex::new();
        cx.st.pir_high = true;
        // a reading that says nothing about `high` must not clobber it
        cx.merge(&msg(pb::envelope::Body::Pir(pb::Pir {
            count: Some(7),
            ..Default::default()
        })));
        assert!(cx.st.pir_high, "absent field clobbered a present truth");
        assert_eq!(cx.st.pir_count, 7);
        // an explicit false is a measurement, and it lands
        cx.merge(&msg(pb::envelope::Body::Pir(pb::Pir {
            high: Some(false),
            count: Some(8),
            ..Default::default()
        })));
        assert!(!cx.st.pir_high, "Some(false) was treated as absence");
    }

    #[test]
    fn pir_count_belongs_to_the_daemon_that_counted() {
        // No invented last-resort increment: the count is a fact about the
        // driver's world. Absence is absence.
        let mut cx = Cortex::new();
        cx.merge(&msg(pb::envelope::Body::Pir(pb::Pir {
            high: Some(true),
            ..Default::default()
        })));
        assert_eq!(cx.st.pir_count, 0);
        cx.merge(&msg(pb::envelope::Body::Pir(pb::Pir {
            high: Some(true),
            count: Some(5),
            ..Default::default()
        })));
        assert_eq!(cx.st.pir_count, 5);
        // explicit zero is a measurement too — a daemon's counter can be 0
        cx.merge(&msg(pb::envelope::Body::Pir(pb::Pir {
            count: Some(0),
            ..Default::default()
        })));
        assert_eq!(cx.st.pir_count, 0);
    }

    #[test]
    fn flip_is_the_default_and_settings_boot_swapped() {
        let st = State::default();
        assert!(st.oled_flip, "the panel is mounted inverted; flipped IS the default");
    }

    #[test]
    fn baro_trend_is_least_squares_not_last_minus_first() {
        // (0,1000) (45,1000) (90,1000.4): last-minus-first is 0.4, which any
        // absolute-hPa threshold could call steady; the slope is 16 hPa/h,
        // over the 0.3 deadband — least squares says rising, and that is the
        // reference's own maths (state.py:_trend_from_samples).
        let mut cx = Cortex::new();
        for (t, p) in [(0.0, 1000.0), (45.0, 1000.0), (90.0, 1000.4)] {
            cx.push_baro(t, p);
        }
        let (dir, series) = cx.baro_trend();
        assert_eq!(dir, "rising", "slope decides direction, not endpoints");
        assert_eq!(series, vec![1000.0, 1000.0, 1000.4]);
    }

    #[test]
    fn baro_window_is_three_hours_and_series_caps_at_sixty() {
        let mut cx = Cortex::new();
        // 61 samples 100 s apart: all inside the 3 h window
        for i in 0..=60 {
            cx.push_baro(i as f64 * 100.0, 1000.0 + i as f64);
        }
        let (_dir, series) = cx.baro_trend();
        assert_eq!(series.len(), 60, "sparkline caps at the last 60");
        assert_eq!(series[0], 1001.0, "oldest of the last 60, oldest first");
        assert_eq!(series[59], 1060.0);

        // Past the 3 h horizon: every aged sample is gone, and the trend
        // carries only the new pair. (ts 16801 prunes everything < 6001.)
        cx.push_baro(16801.0, 9999.0);
        cx.push_baro(16901.0, 9999.5);
        let (_dir, series) = cx.baro_trend();
        assert_eq!(series, vec![9999.0, 9999.5], "the window aged out");
    }

    #[test]
    fn deadband_holds_steady() {
        let mut cx = Cortex::new();
        cx.push_baro(0.0, 1000.0);
        cx.push_baro(90.0, 1000.001); // slope ~0.04 hPa/h, under 0.3
        assert_eq!(cx.baro_trend().0, "steady");
    }

    #[test]
    fn fault_condition_merges_on_presence_only() {
        // Rows are always adopted. The condition is adopted ONLY when the
        // wire carries it — recomputing from rows would erase the one word
        // rows can never say: `mute`. The sweep calling a stale report mute
        // IS its purpose.
        let mut cx = Cortex::new();
        cx.merge(&msg(pb::envelope::Body::Fault(pb::Fault {
            rows: vec![pb::fault::Row {
                level: "warn".into(),
                code: "SENSOR STALE".into(),
                text: "weather quiet".into(),
                face: None,
            }],
            condition: None,
            ts: Some(1.0),
        })));
        assert_eq!(cx.st.faults.len(), 1);
        assert_eq!(cx.st.condition, "well", "no wire condition, no state change");
        assert_eq!(cx.st.fault_ts, Some(1.0));

        cx.merge(&msg(pb::envelope::Body::Fault(pb::Fault {
            rows: vec![],
            condition: Some("mute".into()),
            ts: Some(2.0),
        })));
        assert_eq!(cx.st.condition, "mute", "rows were empty but the sweep spoke");
        assert!(cx.st.faults.is_empty());
    }

    #[test]
    fn published_well_and_renderer_mute_diverge_on_purpose() {
        // The feed's _condition_from([]) says well; the renderer's silence
        // gate says mute. They disagree until a sweep lands, and that gap is
        // the design ("silence is not health"), not a bug to unify away.
        let cx = Cortex::new();
        assert_eq!(Cortex::condition_from(&[]), "well");
        assert_eq!(Cortex::renderer_condition(&cx.st), "mute");
    }

    #[test]
    fn db_down_outranks_niggle() {
        let mut rows = Cortex::injected_faults(true);
        rows.push(FaultRow {
            level: "warn".into(),
            code: "DISK".into(),
            text: "85%".into(),
            face: None,
        });
        assert_eq!(Cortex::condition_from(&rows), "hurts");
    }
}
