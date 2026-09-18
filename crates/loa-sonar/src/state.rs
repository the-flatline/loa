//! The sonar's memory: what it measured, and when that is news.
//!
//! The publish gate is separable from the hardware on purpose — this module
//! has no pins in it and the whole test surface lives here.
use loa_wire::pb;

/// The count and the last range are the daemon's own, per boot; the fields
/// the payload carries are only what was actually measured (proto optional).
#[derive(Default)]
pub struct Snr {
    count: u32,
    cm: Option<f64>,
    ts: Option<f64>,
    /// The last value that WAS published, and when — the move gate reads
    /// these, not the raw cursor.
    last_pub_cm: Option<f64>,
    last_pub_ts: Option<f64>,
}

impl Snr {
    /// The full payload — what an init answer carries.
    pub fn full(&self) -> pb::Sonar {
        pb::Sonar {
            cm: self.cm,
            count: Some(self.count),
            ts: self.ts,
        }
    }

    /// One measurement in; a Sonar message out IF it is news.
    ///
    /// News means: the range moved at least `move_cm` from the last published
    /// value, OR nothing has been published for `quiet_s` (so a still room
    /// still re-asserts the number periodically to a cortex that may have
    /// missed it — but never on a fast tick). With `move_cm` <= 0 every
    /// measurement is news, which is the reference's behaviour: the Python
    /// Sonar.tick published every successful cm at its 1s period.
    /// The count advances on EVERY measurement regardless — it counts what
    /// the sensor measured since boot, not what the wire carried.
    pub fn measured(&mut self, cm: f64, ts: f64, move_cm: f64, quiet_s: f64) -> Option<pb::Sonar> {
        self.count += 1;
        self.cm = Some(cm);
        self.ts = Some(ts);
        let moved = match self.last_pub_cm {
            None => true,
            Some(last) => move_cm <= 0.0 || (cm - last).abs() >= move_cm,
        };
        let stale = match self.last_pub_ts {
            None => true,
            Some(t) => quiet_s > 0.0 && ts - t >= quiet_s,
        };
        if !(moved || stale) {
            return None;
        }
        self.last_pub_cm = Some(cm);
        self.last_pub_ts = Some(ts);
        Some(self.full())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_measurement_is_news_when_the_move_gate_is_zero() {
        // The reference's behaviour, pinned: Sonar.tick published every cm.
        let mut s = Snr::default();
        assert!(s.measured(42.0, 1.0, 0.0, 0.0).is_some());
        assert!(s.measured(42.0, 2.0, 0.0, 0.0).is_some());
        assert_eq!(s.full().count, Some(2), "count counts measurements, not publishes");
    }

    #[test]
    fn a_still_number_holds_its_fire_once_the_gate_opens() {
        let mut s = Snr::default();
        assert!(s.measured(100.0, 1.0, 5.0, 0.0).is_some(), "first is always news");
        assert!(s.measured(101.0, 2.0, 5.0, 0.0).is_none(), "1cm moved < 5cm gate must not publish");
        assert!(s.measured(106.0, 3.0, 5.0, 0.0).is_some(), "6cm past the gate is news");
        // count kept climbing through the silence: 3 measured, 2 published.
        assert_eq!(s.full().count, Some(3));
    }

    #[test]
    fn the_quiet_window_re_asserts_a_still_number_to_a_deaf_cortex() {
        let mut s = Snr::default();
        assert!(s.measured(50.0, 0.0, 5.0, 30.0).is_some());
        assert!(s.measured(50.0, 10.0, 5.0, 30.0).is_none());
        assert!(s.measured(50.0, 30.0, 5.0, 30.0).is_some(), "30s of silence must republish");
    }

    #[test]
    fn the_gate_measures_against_the_last_published_not_the_last_read() {
        // Drift of 3cm, 3cm, 3cm: each step under the 5cm gate, but the first
        // and fourth are 9cm apart. Gating against the last READ would let
        // slow creep vanish entirely; against the last PUBLISH it surfaces.
        let mut s = Snr::default();
        assert!(s.measured(100.0, 1.0, 5.0, 0.0).is_some());
        assert!(s.measured(103.0, 2.0, 5.0, 0.0).is_none());
        assert!(s.measured(106.0, 3.0, 5.0, 0.0).is_some(), "6cm from last published");
    }
}
