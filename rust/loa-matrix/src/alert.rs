//! The alert light: what the body publishes about itself, rendered as one
//! glyph.
//!
//! The panel's job is to be unmissable from across the room at a glance. So
//! this module does one thing: decide which *single* shape the body's own
//! published condition deserves, and hold it.
//!
//! STOPGAP, deliberately. The house contract (see loa-ring, loa-panel) is that
//! the BRAIN decides the tell and the display blits the pixels it is handed.
//! That contract belongs here too, on a `matrix` topic, and it will move there
//! — but the proto and the cortex are both being worked in another window
//! right now, and two writers on one schema is how a wire breaks for a reason
//! nobody can reconstruct later. Until then the mapping lives in one function
//! ([`Body::sigil`]) where it can be read at a glance and moved in one piece.
//!
//! It reads only what the body already says about itself: the `fault` topic
//! ("what hurts, right now") and the `power` topic (the rails, including the
//! throttle word). Nothing here reaches back into the cortex.

use std::thread::sleep;
use std::time::{Duration, Instant};

use loa_topic::{feed_endpoints, pb, Envelope, Subscriber};

use crate::glyphs::{self, Glyph};
use crate::ht16k33::Matrix;

/// What the panel can say, ordered by how much is on fire.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Sigil {
    /// Nothing to report. The resting face, and a state in its own right.
    Dark,
    /// A niggle. Worth knowing, not worth stopping for.
    Attention,
    /// Happening now: a rail out of band, under-voltage live.
    Urgent,
    /// Something that should be running is not.
    Fault,
}

impl Sigil {
    fn glyph(self) -> &'static Glyph {
        match self {
            Sigil::Dark => &glyphs::DARK,
            Sigil::Attention => &glyphs::ATTENTION,
            Sigil::Urgent => &glyphs::URGENT,
            Sigil::Fault => &glyphs::FAULT,
        }
    }

    /// Half a blink period. Zero means steady — a state that is holding, as
    /// opposed to one that needs you now.
    fn half_period(self) -> Duration {
        match self {
            Sigil::Attention => Duration::from_millis(700),
            Sigil::Urgent => Duration::from_millis(150),
            _ => Duration::ZERO,
        }
    }

    /// How loud the panel is. The chip dims globally, so this is the one
    /// channel that can carry "how much" across all 64 pixels at once — the
    /// same division of labour as shape ("what") and motion ("when"). Resting
    /// is genuinely OFF, not dim: a panel that is always lit stops being
    /// information.
    fn level(self) -> u8 {
        match self {
            Sigil::Dark => 0,
            Sigil::Attention => 4,
            Sigil::Urgent => 9,
            Sigil::Fault => 12,
        }
    }
}

/// Everything the feed has told us, as of the last frame.
#[derive(Default)]
pub struct Body {
    pub faults: u32,
    pub warns: u32,
    pub condition: String,
    pub throttled: u32,
}

impl Body {
    /// Absorb one message. Unknown topics are ignored on purpose: this display
    /// does not need to understand the whole wire to show one shape.
    fn absorb(&mut self, env: &Envelope) {
        match env.env.body.as_ref() {
            Some(pb::envelope::Body::Fault(f)) => {
                if let Some(c) = &f.condition {
                    self.condition = c.clone();
                }
                // The rows are the truth; the counts are derived from them, not
                // carried alongside, so they cannot disagree with each other.
                self.faults = 0;
                self.warns = 0;
                for row in &f.rows {
                    match row.level.as_str() {
                        "fault" => self.faults += 1,
                        "warn" => self.warns += 1,
                        _ => {}
                    }
                }
            }
            Some(pb::envelope::Body::Power(p)) => {
                if let Some(word) = p.rails.get("throttled") {
                    self.throttled = *word as u32;
                }
            }
            _ => {}
        }
    }

    /// The whole policy, in priority order. Highest thing that is true wins.
    pub fn sigil(&self) -> Sigil {
        if self.faults > 0 || self.condition == "hurts" {
            return Sigil::Fault;
        }
        // Bit 0 of the throttle word is under-voltage HAPPENING NOW; the high
        // word is history and is not worth a lit panel.
        if self.throttled & 0x1 == 1 {
            return Sigil::Urgent;
        }
        if self.warns > 0 || self.condition == "niggle" {
            return Sigil::Attention;
        }
        Sigil::Dark
    }
}

/// Run the alert light. `secs` of zero means forever.
///
/// `bright` is the starting level only: once running, the state owns the
/// brightness (`Sigil::level`), because "how loud" is part of what the panel is
/// saying.
pub fn run(m: &mut Matrix, secs: f64, bright: u8) -> Result<Body, Box<dyn std::error::Error>> {
    let endpoints = feed_endpoints();
    let mut sub = match Subscriber::connect(&endpoints, &["fault", "power"]) {
        Ok(s) => s,
        Err(e) => return Err(format!("no feed at {endpoints:?}: {e}").into()),
    };
    m.brightness(bright)?;

    let mut body = Body::default();
    let started = Instant::now();
    let mut shown: Option<Sigil> = None;
    let mut lit = false;
    let mut edge = Instant::now();
    let mut changes = 0u32;

    loop {
        match sub.drain() {
            Ok(batch) => {
                for env in &batch {
                    body.absorb(env);
                }
            }
            Err(e) => eprintln!("alert: feed error: {e}"),
        }

        let now = Instant::now();
        let sigil = body.sigil();
        let half = sigil.half_period();

        if shown != Some(sigil) {
            // A new state shows IMMEDIATELY. Waiting for the next blink edge
            // makes the panel look broken at the exact moment it has news.
            shown = Some(sigil);
            changes += 1;
            lit = true;
            edge = now + half;
            m.brightness(sigil.level())?;
            m.glyph(sigil.glyph());
            m.flush()?;
        } else if !half.is_zero() && now >= edge {
            lit = !lit;
            if lit {
                m.glyph(sigil.glyph());
            } else {
                m.clear();
            }
            m.flush()?;
            edge = now + half;
        }

        if secs > 0.0 && started.elapsed().as_secs_f64() >= secs {
            break;
        }
        sleep(Duration::from_millis(40));
    }

    m.clear();
    m.flush()?;
    println!(
        "alert: {} state change(s) in {:.1}s — {} fault(s), {} warn(s), condition {:?}, throttled 0x{:x}",
        changes,
        started.elapsed().as_secs_f64(),
        body.faults,
        body.warns,
        body.condition,
        body.throttled
    );
    Ok(body)
}
