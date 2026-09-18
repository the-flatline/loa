//! The merged air: what the daemon measured, and when it is news.
//!
//! THE DOCTRINE THIS FILE ENFORCES. Weather is ONE payload (v10): the DHT
//! half (temp_c, hum_pct) and the baro half (pressure_hpa) ride the same
//! message, and `count` is the daemon's own — the DHT frames it decoded since
//! boot, because the DHT's 1Hz tick is the air's heartbeat. `trend` and
//! `series` are NEVER set here: they are the cortex's derivation from its
//! sample window, and a daemon that computed what three measurements mean is
//! the limb driving the brain.
//!
//! THE PUBLISH GATE — "publish when a field MOVES". The reference (the
//! sense.py-era drivers) published every valid read at its own period: the
//! DHT decoded, it published; the baro answered, it published. There was no
//! threshold to carry forward, so this gate is the doctrine's shape with the
//! reference's numbers where it has any:
//!   - every VALID reading is a publish (both references published unconditionally),
//!   - BUT a reading that repeats the last published number exactly is
//!     suppressed — the wire carries news, and the same number twice is not
//!     news. The DHT's own quantisation (0.1 deg, 0.1 rh) means it genuinely
//!     repeats at rest; the baro's Pa-level output almost never does, so in
//!     practice "pressure moves publish too" holds by construction.
//!   - the count that would have ridden the suppressed frame is NOT lost: the
//!     next publish carries it whole (count counts what was measured, the
//!     last partial was only its envelope).
use loa_wire::pb;

/// Gate for a repeat: exactly-equal fields are silence. Anything that moves
/// at the sensor's own quantisation (0.1 here, the DHT's step) is news.
fn moved(a: f64, b: f64) -> bool {
    (a - b).abs() >= 0.05 // half the 0.1 quantisation step: a real step moves, a repeat doesn't
}

#[derive(Default)]
pub struct Air {
    // the last MEASURED values (what `full` answers with)
    temp_c: Option<f64>,
    hum_pct: Option<f64>,
    baro_c: Option<f64>, // the baro's own temp — measured, not on the wire
    pressure_hpa: Option<f64>,
    ts: Option<f64>, // the last published stamp (cortex maps Weather.ts to both temp_ts and baro_ts — the fold's known wart)
    count: u32, // DHT frames decoded since boot; the air's heartbeat count
    // the last PUBLISHED numbers, the gate's memory
    pub_sent: Option<f64>,
    hum_sent: Option<f64>,
    pres_sent: Option<f64>,
}

pub enum News {
    /// Publish this partial payload (only the fields that moved).
    Partial(pb::Weather),
}

impl Air {
    /// A DHT frame decoded: the count climbs, and temp/hum are news when they
    /// moved (the first frame always is). A valid-but-repeating frame updates
    /// the memory silently — its number rides the next publish.
    pub fn dht(&mut self, temp_c: f64, hum_pct: f64, ts: f64) -> Vec<News> {
        self.count += 1;
        self.temp_c = Some(temp_c);
        self.hum_pct = Some(hum_pct);
        let temp_news = !self.pub_sent.map(|s| !moved(temp_c, s)).unwrap_or(false);
        let hum_news = !self.hum_sent.map(|s| !moved(hum_pct, s)).unwrap_or(false);
        if !temp_news && !hum_news {
            return vec![]; // a repeat at rest: the count waits for the next news
        }
        let mut m = pb::Weather {
            ts: Some(ts),
            count: Some(self.count),
            ..Default::default()
        };
        if temp_news {
            m.temp_c = Some(temp_c);
            self.pub_sent = Some(temp_c);
        }
        if hum_news {
            m.hum_pct = Some(hum_pct);
            self.hum_sent = Some(hum_pct);
        }
        self.ts = Some(ts);
        vec![News::Partial(m)]
    }

    /// A baro reading: pressure is news when it moved (it almost always has,
    /// at Pa resolution — the gate exists for the DHT's quantised repeats, not
    /// to mute the barometer). The baro's own temp is not on the v10 wire:
    /// Weather carries temp_c meaning the DHT's, so baro temp is measured,
    /// remembered for `full`'s sake, and never published as temp_c.
    pub fn baro(&mut self, _baro_temp_c: f64, pressure_hpa: f64, ts: f64) -> Vec<News> {
        self.baro_c = Some(_baro_temp_c);
        let news = !self.pres_sent.map(|s| !moved(pressure_hpa, s)).unwrap_or(false);
        self.pressure_hpa = Some(pressure_hpa);
        if !news {
            return vec![];
        }
        let mut m = pb::Weather {
            pressure_hpa: Some(pressure_hpa),
            ts: Some(ts),
            ..Default::default()
        };
        // The count rides every publish that knows it — if the DHT half has
        // spoken, a pressure-only frame restates it so the wire never shows a
        // number going backwards mid-window.
        if self.count > 0 {
            m.count = Some(self.count);
        }
        self.pres_sent = Some(pressure_hpa);
        self.ts = Some(ts);
        vec![News::Partial(m)]
    }

    /// The answer to an init ask: everything measured, under every field.
    /// Fields never measured are absent — presence-only, send only what was
    /// measured. trend/series: never (the cortex's derivation).
    pub fn full(&self) -> pb::Weather {
        pb::Weather {
            temp_c: self.temp_c,
            hum_pct: self.hum_pct,
            ts: self.ts,
            count: Some(self.count),
            pressure_hpa: self.pressure_hpa,
            trend: None,
            series: vec![],
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_of(m: &pb::Weather) -> Option<f64> {
        m.temp_c
    }

    #[test]
    fn the_first_dht_frame_is_always_news_with_count_one() {
        let mut a = Air::default();
        let news = a.dht(21.5, 40.0, 1000.0);
        assert_eq!(news.len(), 1, "a first reading cannot be a repeat");
        let News::Partial(m) = &news[0];
        assert_eq!(temp_of(m), Some(21.5));
        assert_eq!(m.hum_pct, Some(40.0));
        assert_eq!(m.count, Some(1));
        assert_eq!(m.pressure_hpa, None, "the DHT half must not invent pressure");
    }

    #[test]
    fn a_repeating_dht_frame_is_silence_and_its_count_rides_the_next_news() {
        // The DHT's 0.1 quantisation genuinely repeats at rest; the wire is
        // news only. The count must not be lost by the quiet frames.
        let mut a = Air::default();
        assert_eq!(a.dht(21.5, 40.0, 1000.0).len(), 1);
        assert!(a.dht(21.5, 40.0, 1001.0).is_empty(), "an exact repeat is not news");
        assert!(a.dht(21.5, 40.0, 1002.0).is_empty());
        let news = a.dht(21.6, 40.0, 1003.0); // moved at the quantisation step
        let News::Partial(m) = &news[0];
        assert_eq!(m.temp_c, Some(21.6));
        assert_eq!(m.hum_pct, None, "humidity did not move");
        assert_eq!(m.count, Some(4), "three quiet frames, one loud — count says four");
    }

    #[test]
    fn half_a_quantisation_step_is_not_a_move_and_a_step_is() {
        let mut a = Air::default();
        a.dht(21.5, 40.0, 1000.0);
        assert!(a.dht(21.54, 40.0, 1001.0).is_empty(), "0.04deg drift is the spin's noise, not weather");
        assert!(!a.dht(21.6, 40.0, 1002.0).is_empty(), "0.1deg IS the sensor's own step");
    }

    #[test]
    fn pressure_moves_publish_and_feed_the_trend_window() {
        let mut a = Air::default();
        a.dht(21.5, 40.0, 1000.0); // the heartbeat starts
        let news = a.baro(22.9, 1022.76, 1001.0);
        assert_eq!(news.len(), 1, "pressure that arrives is pressure that moved");
        let News::Partial(m) = &news[0];
        assert_eq!(m.pressure_hpa, Some(1022.76));
        assert_eq!(m.temp_c, None, "the baro's temp must never ride temp_c — that field is the DHT's truth");
        assert_eq!(m.hum_pct, None);
        assert_eq!(m.count, Some(1), "the heartbeat count rides along");
        // a repeated Pa-exact pressure (the chip asleep mid-wire) is silence
        assert!(a.baro(22.9, 1022.76, 1002.0).is_empty());
    }

    #[test]
    fn the_full_answer_carries_everything_measured_and_never_a_trend() {
        let mut a = Air::default();
        a.dht(21.5, 40.0, 1000.0);
        a.baro(22.9, 1022.76, 1001.0);
        let f = a.full();
        assert_eq!(f.temp_c, Some(21.5));
        assert_eq!(f.hum_pct, Some(40.0));
        assert_eq!(f.pressure_hpa, Some(1022.76));
        assert_eq!(f.count, Some(1));
        assert_eq!(f.trend, None, "trend is the cortex's derivation, never the daemon's");
        assert!(f.series.is_empty(), "series is the cortex's derivation, never the daemon's");
    }

    #[test]
    fn a_daemon_that_measured_nothing_answers_with_only_a_zero_count() {
        // Presence-only: the fresh daemon's full payload must not fabricate
        // air. count Some(0) is honest ("measured nothing yet"); the numbers
        // are absent, and the cortex's merge leaves its own memory untouched.
        let f = Air::default().full();
        assert_eq!(f.count, Some(0));
        assert_eq!(f.temp_c, None);
        assert_eq!(f.hum_pct, None);
        assert_eq!(f.pressure_hpa, None);
        assert_eq!(f.ts, None);
    }
}
