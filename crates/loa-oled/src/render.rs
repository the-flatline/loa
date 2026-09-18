//! Frame assembly: state in, 1024 bytes out — the port of the reference's
//! frames.FaceRenderer. Holds the animation object and the wash clock.
//!
//! ONE instance lives in the daemon: the renderers carry their own clocks and
//! a Scope's blips are its own memory, so they are rebuilt only when the key
//! changes — (mode, text) — never per frame.
//!
//! The rules this keeps, by construction:
//!   * PURE. No hardware, no wire. The daemon blits what this returns.
//!   * The bytes are the whole state: exactly 1024, page-major — the same
//!     number the wire and the glass read (loa-common::geom).
//!   * The face_state takeover is applied HERE, at render time, and never
//!     written back to the held state: the picture says where it hurts, the
//!     state stays what the brain published.

use rand::rngs::StdRng;
use rand::{Rng, SeedableRng};

use crate::face::{self, Animation};
use crate::frame::{Buf, Frame};
use crate::rstate::{face_state, RenderState};

/// Pixel wash: a full-frame exercise every few minutes so no pattern holds
/// long enough to ghost (Divv's suggestion). It is rendered HERE, like
/// everything else, so the glass shows only what this renderer drew — a wash
/// the daemon drew itself would be a second drawer and a second picture.
pub const WASH_EVERY_S: f64 = 300.0;
pub const WASH_SECS: f64 = 5.0;

/// A Frame and the clocks that decide what gets drawn on it.
pub struct FaceRenderer {
    frame: Frame,
    anim: Option<Box<dyn Animation>>,
    key: Option<(String, Option<String>)>,
    last_t: f64,
    pub next_wash: f64,
    wash_until: f64,
    wash_blink: u32,
    rng: StdRng,
}

impl FaceRenderer {
    pub fn new(now: f64) -> Self {
        FaceRenderer {
            frame: Frame::new(),
            anim: None,
            key: None,
            last_t: now,
            next_wash: now + WASH_EVERY_S,
            wash_until: 0.0,
            wash_blink: 0,
            rng: StdRng::seed_from_u64(now.to_bits()),
        }
    }

    /// One frame: the held state and the wall clock go in, 1024 bytes come
    /// out. `off` draws nothing — a black field — but still returns bytes,
    /// because the feed always carries a whole frame.
    pub fn render(&mut self, st: &RenderState, t: f64) -> Buf {
        let st = face_state(st.clone());
        let dt = t - self.last_t;
        self.last_t = t;
        let mode = if st.mode.is_empty() { "scope" } else { &st.mode };

        // the wash: exercise every pixel for WASH_SECS, every WASH_EVERY_S.
        // Skips "off" — a blank face is not forming retention.
        if mode != "off" && t >= self.next_wash {
            self.wash_until = t + WASH_SECS;
            self.next_wash = t + WASH_EVERY_S;
            self.wash_blink = 0;
        }
        if t < self.wash_until {
            return self.wash(t);
        }

        let key = (mode.to_string(), st.text.clone());
        if self.key.as_ref() != Some(&key) {
            self.key = Some(key);
            self.anim = face::make_renderer(mode, &st, t, self.rng.clone());
        }
        self.frame.clear();
        if let Some(a) = self.anim.as_mut() {
            a.tick(dt, t);
            a.draw(&mut self.frame, t, &st);
        }
        self.frame.frame_bytes()
    }

    /// Full-field blink, then dense static. Every pixel driven, and driven
    /// through the topic like every other frame.
    fn wash(&mut self, t: f64) -> Buf {
        if t - (self.wash_until - WASH_SECS) < 2.0 {
            let v = if self.wash_blink % 2 == 0 { 0xFF } else { 0x00 };
            self.wash_blink += 1;
            for b in self.frame.buf.iter_mut() {
                *b = v;
            }
        } else {
            for b in self.frame.buf.iter_mut() {
                *b = self.rng.gen();
            }
        }
        self.frame.frame_bytes()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::rstate::FaultRow;
    use loa_wire::pb;

    fn scope_state() -> RenderState {
        let mut st = RenderState::default();
        st.mode = "scope".into();
        // the takeover fires on mute until a fault is heard: say well here
        // so the test renders the face it names
        st.fault_seen = true;
        st.condition = "well".into();
        st
    }

    #[test]
    fn the_key_rebuild_rule_holds_stateful_objects() {
        let mut r = FaceRenderer::new(1000.0);
        let st = scope_state();
        let before = face::scope_builds();
        let _ = r.render(&st, 1001.0);
        let _ = r.render(&st, 1002.0);
        // scope then scope: same object, nobody was rebuilt
        assert_eq!(face::scope_builds(), before + 1);
        // scope then ecg then scope: the scope comes back NEW
        let mut ecg = st.clone();
        ecg.mode = "ecg".into();
        let _ = r.render(&ecg, 1003.0);
        let _ = r.render(&st, 1004.0);
        assert_eq!(face::scope_builds(), before + 2);
    }

    #[test]
    fn text_rides_the_key_so_a_new_slogan_rebuilds_the_marquee() {
        let mut r = FaceRenderer::new(1000.0);
        let mut st = scope_state();
        st.mode = "text".into();
        st.text = Some("ONE".into());
        let _ = r.render(&st, 1001.0);
        let k1 = r.key.clone();
        st.text = Some("TWO".into());
        let _ = r.render(&st, 1002.0);
        assert_ne!(k1.unwrap().1, Some("TWO".into()));
    }

    #[test]
    fn a_fault_says_hurts_and_the_face_names_where() {
        // a fault at the hurts level is what the takeover reads — and the
        // takeover rule is exactly the reference's, so an idle face flips to
        // the pain page
        let mut st = RenderState::default();
        st.mode = "scope".into();
        st.fault_seen = true;
        st.condition = "hurts".into();
        let _ = scope_state(); // keep the helper used in this module's eyes
        let buf = FaceRenderer::new(1000.0).render(&st, 1000.5);
        assert!(buf.iter().any(|&b| b != 0), "the pain page drew nothing");
    }

    #[test]
    fn silence_is_not_health_the_mute_gate_takes_the_face() {
        // no fault EVER heard: the renderer's condition is mute, and a mute
        // body takes its own face over to the pain page even at the bench
        let st = RenderState::default(); // mode empty -> scope by default
        assert!(!st.fault_seen);
        let buf = FaceRenderer::new(1000.0).render(&st, 1000.5);
        assert!(buf.iter().any(|&b| b != 0));
    }

    #[test]
    fn the_wash_fires_at_the_clock_edge_blinks_then_goes_static() {
        let mut r = FaceRenderer::new(0.0);
        let st = scope_state();
        // before the edge: a scope line, not a full field
        let plain = r.render(&st, 299.0);
        assert!(plain.iter().any(|&b| b != 0) && plain.iter().any(|&b| b != 0xFF));
        // at the edge: the FIRST wash frame is the full-field blink, 0xff
        let w0 = r.render(&st, 300.0);
        assert!(w0.iter().all(|&b| b == 0xFF));
        // the next frame alternates to black
        let w1 = r.render(&st, 300.05);
        assert!(w1.iter().all(|&b| b == 0x00));
        // past 2s of blink: dense static, and the clock moved on the wash
        let w2 = r.render(&st, 303.0);
        assert_ne!(w2, w1);
        // the wash ends and the face returns
        let after = r.render(&st, 306.0);
        assert!(after.iter().any(|&b| b != 0xFF));
    }

    #[test]
    fn off_is_black_and_never_washes() {
        let mut st = scope_state();
        st.mode = "off".into();
        let mut r = FaceRenderer::new(0.0);
        let b = r.render(&st, 0.5);
        assert!(b.iter().all(|&x| x == 0));
        // the wash clock must not arm for an off face
        let b = r.render(&st, 400.0);
        assert!(b.iter().all(|&x| x == 0), "off washed anyway");
    }

    #[test]
    fn the_takeover_reaches_the_renderer_not_the_state() {
        // render-side only: the caller's RenderState keeps its mode
        let mut st = RenderState::default();
        st.mode = "scope".into();
        st.fault = Some(crate::rstate::FaultMsg {
            rows: vec![FaultRow {
                level: "fault".into(),
                code: "RING DARK".into(),
                text: String::new(),
                face: String::new(),
            }],
            condition: Some("hurts".into()),
            ts: 1000.0,
        });
        let _ = FaceRenderer::new(1000.0).render(&st, 1000.5);
        assert_eq!(st.mode, "scope", "state was written back");
        // and the held readings survive a merge from the wire
        let mut st2 = RenderState::default();
        st2.hold(
            "sonar",
            &pb::Envelope {
                schema_version: loa_wire::SCHEMA_VERSION,
                body: Some(pb::envelope::Body::Sonar(pb::Sonar {
                    cm: Some(10.0),
                    count: Some(1),
                    ts: Some(1.0),
                })),
            },
        );
        assert_eq!(st2.snr_cm, Some(10.0));
    }
}
