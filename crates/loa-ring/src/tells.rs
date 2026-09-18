//! The tell: which voice the ring speaks, and the animation maths behind it.
//! Carried from `loa/cortex/ring.py` and `loa/ring/animations.py` (READ ONLY
//! references — line refs below point at those files).
//!
//! The tell, highest first (ring.py:10): alarm > hurts > mute > busy >
//! one-shot > home.

use loa_common::condition;

/// The animation clock (ring.py:25: FPS = 30). The time-based sequences are
/// built at this rate; the dither's maths rides on every render call, which
/// is the DAEMON's clock, not this one.
pub const FPS: f64 = 30.0;

/// HOME_PEAK / BUSY_PEAK / BASE (animations.py:24-26).
pub const HOME_PEAK: f64 = 35.0;
pub const BUSY_PEAK: f64 = 55.0;
pub const BASE: f64 = 3.0;

/// hurts: the breath STOPS and holds a deep red pulse — (bright x3, dim x4),
/// 0.28s a step (ring.py:57-61). Deliberately not the alarm flash.
pub const HURTS: [f64; 7] = [0.85, 0.85, 0.85, 0.18, 0.18, 0.18, 0.18];
pub const HURTS_STEP_S: f64 = 0.28;
pub const HURTS_S: f64 = 0.28 * 7.0; // ring.py:61

/// alarm: three red pulses with gaps, then a rest — 0.4s on, 0.3s off, and a
/// 2.0s hold at the end of the cycle (ring.py:63-65).
pub const ALARM_CYCLE_S: f64 = 3.0 * 0.7 + 2.0;

/// mute: dark, one dim blink every 10s, the last 0.4s of each 10s (ring.py:67-70).
pub const MUTE_S: f64 = 10.0;
pub const BLINK_S: f64 = 0.4;

/// NIGGLE_PERIOD_S (ring.py:28); the tick itself is 0.36s, bright 0.55 for
/// its first 0.18s then 0.10 (ring.py:155-159).
pub const NIGGLE_PERIOD_S: f64 = 20.0;
pub const NIGGLE_S: f64 = 0.36;

/// Breath phase totals: home 3.0 + 0.8 + 4.2 + 0.5 (ring.py:73, from
/// animations.py:60), busy 2.4 + 0.5 + 3.4 + 0.4 (ring.py:75, animations.py:123).
pub const HOME_S: f64 = 3.0 + 0.8 + 4.2 + 0.5;
pub const BUSY_S: f64 = 2.4 + 0.5 + 3.4 + 0.4;

/// glitch one-shot length (ring.py:77).
pub const GLITCH_S: f64 = 0.3;

/// The condition vocabulary — the words live in loa-common (condition.rs:9-12);
/// this is the ring's view of them.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Cond {
    Well,
    Niggle,
    Hurts,
    Mute,
}

impl Cond {
    pub fn parse(s: &str) -> Cond {
        match s {
            condition::HURTS => Cond::Hurts,
            condition::MUTE => Cond::Mute,
            condition::NIGGLE => Cond::Niggle,
            _ => Cond::Well,
        }
    }
}

/// The mute gate is the DEVICE's (ring.py:31-40): a fault report that has
/// never been heard reads as mute, never as well — silence is not health.
/// Whatever the state's or fault's word says, no fault message means mute.
pub fn device_condition(fault_seen: bool, cond_word: Option<&str>) -> Cond {
    if !fault_seen {
        return Cond::Mute; // ring.py:38-39
    }
    Cond::parse(cond_word.unwrap_or(condition::WELL)) // ring.py:40: `or "well"`
}

/// One render input: what the wire says, as one snapshot.
#[derive(Debug, Clone)]
pub struct RingInput {
    /// Already gated by `device_condition` — the caller decides, because only
    /// the device knows whether it has ever heard a fault.
    pub cond: Cond,
    /// state.ring_state, "home" when absent (ring.py:111).
    pub ring_state: String,
    /// state.ripperdoc — the console flag; it switches the scan cadence
    /// (ring.py:177): (0.9, 0.3) when the console is on, else (1.6, 0.5).
    pub ripperdoc: bool,
}

/// The six tells; scan and glitch are one-shot kinds. The kind names the
/// dither instance: one per tell, reset when it changes (ring.py:127-129).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Tell {
    Alarm,
    Hurts,
    Mute,
    Busy,
    Scan,
    Glitch,
    Home,
}

/// Precedence, ring.py:113-126, verbatim order, decision only — no frames,
/// no state. `pending` is the unexpired one-shot record (Scan or Glitch);
/// a one-shot plays ONLY while ring_state == "home" (ring.py:166), and a
/// ring_state of "busy"/"alarm" outranks it.
pub fn pick_tell(input: &RingInput, pending: Option<Tell>) -> Tell {
    if input.ring_state == "alarm" {
        Tell::Alarm // ring.py:113
    } else if input.cond == Cond::Hurts {
        Tell::Hurts // ring.py:115
    } else if input.cond == Cond::Mute {
        Tell::Mute // ring.py:117
    } else if input.ring_state == "busy" {
        Tell::Busy // ring.py:119
    } else if input.ring_state == "home" {
        pending.unwrap_or(Tell::Home) // ring.py:122-126
    } else {
        // ring.py:121-126: a ring_state that is neither alarm/busy/home
        // ("scan", "glitch" — states, not records) gets no one-shot (the
        // gate in _oneshot) and falls through to home.
        Tell::Home
    }
}

// ---------------------------------------------------------------------------
// The maths, straight out of loa/ring/animations.py.
// ---------------------------------------------------------------------------

/// hsv (animations.py:29-40): h degrees, s/v 0..1 -> (r,g,b) floats 0..255
/// in PERCEPTUAL space. No truncation here — truncation is the renderer's
/// job and it does it with error diffusion (animations.py:5-6).
pub fn hsv(h: f64, s: f64, v: f64) -> (f64, f64, f64) {
    let c = v * s;
    let x = c * (1.0 - ((h / 60.0) % 2.0 - 1.0).abs());
    let m = v - c;
    let (r, g, b) = if h < 60.0 {
        (c, x, 0.0)
    } else if h < 120.0 {
        (x, c, 0.0)
    } else if h < 180.0 {
        (0.0, c, x)
    } else if h < 240.0 {
        (0.0, x, c)
    } else if h < 300.0 {
        (x, 0.0, c)
    } else {
        (c, 0.0, x)
    };
    ((r + m) * 255.0, (g + m) * 255.0, (b + m) * 255.0)
}

/// _solid (ring.py:52-54): one colour across the whole ring.
pub fn solid(h: f64, s: f64, v: f64) -> Vec<(f64, f64, f64)> {
    vec![hsv(h, s, v); 24]
}

/// _eased_curve (animations.py:43-56): the float brightness curve of one
/// breath phase at FPS. kind: inhale/hold/exhale/rest.
fn eased_curve(secs: f64, peak: f64, base: f64, kind: &str) -> Vec<f64> {
    let n = (secs * FPS).round().max(1.0) as usize;
    use std::f64::consts::PI;
    match kind {
        "hold" => vec![peak; n],
        "rest" => vec![base; n],
        "inhale" => (0..n)
            .map(|t| base + (peak - base) * (t as f64 / n as f64 * PI * 0.5).sin().powf(1.5))
            .collect(),
        "exhale" => (0..n)
            .map(|t| base + (peak - base) * (t as f64 / n as f64 * PI * 0.5).cos().powf(1.5))
            .collect(),
        _ => panic!("bad breath phase {kind}"),
    }
}

/// One breath cycle's brightness curve (animations.py:59-68 flattened: every
/// LED gets the same value, so the curve IS the frame).
fn breath_curve(peak: f64, inhale_s: f64, hold_s: f64, exhale_s: f64, rest_s: f64) -> Vec<f64> {
    let mut v = eased_curve(inhale_s, peak, BASE, "inhale");
    v.extend(eased_curve(hold_s, peak, BASE, "hold"));
    v.extend(eased_curve(exhale_s, peak, BASE, "exhale"));
    v.extend(eased_curve(rest_s, peak, BASE, "rest"));
    v
}

/// _HOME_BREATH (ring.py:72): peak HOME_PEAK, default phases, hue applied at
/// sample time by home_hue (ring.py:162).
pub fn home_curve() -> Vec<f64> {
    breath_curve(HOME_PEAK, 3.0, 0.8, 4.2, 0.5)
}

/// _BUSY_BREATH (ring.py:74): busy_frames — peak BUSY_PEAK, phases
/// 2.4/0.5/3.4/0.4, hue 45 (animations.py:121-124).
pub fn busy_curve() -> Vec<f64> {
    breath_curve(BUSY_PEAK, 2.4, 0.5, 3.4, 0.4)
}

/// home_hue (animations.py:116-118): slow green -> cyan drift, one full
/// cycle per 60s.
pub fn home_hue(t: f64) -> f64 {
    120.0 + 45.0 * (0.5 - 0.5 * (2.0 * std::f64::consts::PI * t / 60.0).cos())
}

/// _sample (ring.py:80-83): the element of a time-based sequence at time t,
/// wrapping. Scalar version.
pub fn sample_scalar(seq: &[f64], t: f64, total_s: f64) -> f64 {
    let i = ((t % total_s) / total_s * seq.len() as f64) as usize;
    seq[i.min(seq.len() - 1)]
}

/// _sample for frame sequences.
pub fn sample_frames(seq: &[Vec<(f64, f64, f64)>], t: f64, total_s: f64) -> Vec<(f64, f64, f64)> {
    let i = ((t % total_s) / total_s * seq.len() as f64) as usize;
    seq[i.min(seq.len() - 1)].clone()
}

/// scan_frames (animations.py:71-101): one comet lap — head travels
/// continuously; the fade dissolves the tail in place. hue 150, s 0.7,
/// peak 120, tail window d < 6 with exp(-d * 0.9).
pub fn scan_frames(lap_s: f64, fade_s: f64) -> Vec<Vec<(f64, f64, f64)>> {
    let led_count = 24usize;
    let mut frames = Vec::new();
    let lap_n = (lap_s * FPS).round().max(1.0) as usize;
    let head_step = led_count as f64 / lap_n as f64;
    let mut lap_frames = Vec::new();
    for s in 0..lap_n {
        let head = s as f64 * head_step;
        lap_frames.push(comet_frame(head, 1.0));
    }
    frames.extend(lap_frames);
    let final_head = (lap_n - 1) as f64 * head_step;
    let fade_n = (fade_s * FPS).round().max(1.0) as usize;
    for s in 0..fade_n {
        let amp = 1.0 - s as f64 / fade_n as f64;
        frames.push(comet_frame(final_head, amp));
    }
    frames
}

fn comet_frame(head: f64, amp: f64) -> Vec<(f64, f64, f64)> {
    let mut frame = vec![(0.0, 0.0, 0.0); 24];
    for i in 0..24usize {
        let d = (head - i as f64).rem_euclid(24.0);
        if d < 6.0 {
            let b = 120.0 * (-d * 0.9).exp() * amp; // animations.py:83,96
            frame[i] = hsv(150.0, 0.7, b / 255.0);
        }
    }
    frame
}

/// glitch_frames (animations.py:104-113): RGB-split stutter — each LED
/// randomly red or cyan, `coin` supplies the choice (Python draws from
/// `random`; the pattern is noise by design, the SHAPE is what matters),
/// then one dark frame closes it (animations.py:112).
pub fn glitch_frames(coin: impl FnMut() -> bool) -> Vec<Vec<(f64, f64, f64)>> {
    let mut coin = coin;
    let n = (GLITCH_S * FPS).round().max(1.0) as usize; // 9 frames at 30fps
    let mut frames = Vec::new();
    for _ in 0..n {
        let frame = (0..24)
            .map(|_| {
                if coin() {
                    (255.0, 0.0, 0.0)
                } else {
                    (0.0, 255.0, 255.0)
                }
            })
            .collect();
        frames.push(frame);
    }
    frames.push(vec![(0.0, 0.0, 0.0); 24]);
    frames
}

/// The scan timings, ring.py:177: `(0.9, 0.3)` when the console runs, else
/// `(1.6, 0.5)`. One function so the switch is testable.
pub fn scan_timings(ripperdoc: bool) -> (f64, f64) {
    if ripperdoc {
        (0.9, 0.3)
    } else {
        (1.6, 0.5)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scan_timings_switch_on_the_console_flag() {
        // ring.py:177
        assert_eq!(scan_timings(true), (0.9, 0.3));
        assert_eq!(scan_timings(false), (1.6, 0.5));
        // The sequences really differ: 30fps, round(lap*fps)+round(fade*fps).
        assert_eq!(scan_frames(1.6, 0.5).len(), 48 + 15); // 63 — golden
        assert_eq!(scan_frames(0.9, 0.3).len(), 27 + 9); // 36 — golden
    }

    #[test]
    fn glitch_frames_are_nine_noise_plus_dark() {
        // animations.py:104-113 at 30fps: round(0.3/1000*1000... ms=300 ->
        // n = 9 stutter frames + 1 dark = 10 (golden glitch_len).
        let seq = glitch_frames(|| true);
        assert_eq!(seq.len(), 10);
        assert_eq!(seq[9], vec![(0.0, 0.0, 0.0); 24]);
        for px in &seq[0] {
            assert!(*px == (255.0, 0.0, 0.0) || *px == (0.0, 255.0, 255.0));
        }
    }

    #[test]
    fn precedence_is_the_ladder_of_ring_py() {
        let inp = |rs: &str, c: Cond, rd: bool| RingInput {
            cond: c,
            ring_state: rs.into(),
            ripperdoc: rd,
        };
        // alarm beats all: even hurts, even a live one-shot.
        assert_eq!(
            pick_tell(&inp("alarm", Cond::Hurts, false), Some(Tell::Scan)),
            Tell::Alarm
        );
        // hurts beats busy.
        assert_eq!(
            pick_tell(&inp("busy", Cond::Hurts, false), None),
            Tell::Hurts
        );
        // hurts beats a live one-shot too (ring.py:115 before :122).
        assert_eq!(
            pick_tell(&inp("home", Cond::Hurts, false), Some(Tell::Glitch)),
            Tell::Hurts
        );
        // mute beats busy.
        assert_eq!(pick_tell(&inp("busy", Cond::Mute, false), None), Tell::Mute);
        // mute beats a live one-shot.
        assert_eq!(
            pick_tell(&inp("home", Cond::Mute, false), Some(Tell::Scan)),
            Tell::Mute
        );
        // busy beats a live one-shot (ring.py:119 before :122).
        assert_eq!(
            pick_tell(&inp("busy", Cond::Well, false), Some(Tell::Scan)),
            Tell::Busy
        );
        // one-shot plays ONLY at home (ring.py:166): a record pending while
        // the state asks for "scan"/"glitch" is gated — the state word is
        // NOT the record — and home with nothing pending is home.
        assert_eq!(
            pick_tell(&inp("home", Cond::Well, false), Some(Tell::Scan)),
            Tell::Scan
        );
        assert_eq!(
            pick_tell(&inp("home", Cond::Well, false), Some(Tell::Glitch)),
            Tell::Glitch
        );
        assert_eq!(
            pick_tell(&inp("scan", Cond::Well, false), Some(Tell::Scan)),
            Tell::Home
        );
        assert_eq!(
            pick_tell(&inp("glitch", Cond::Well, false), Some(Tell::Glitch)),
            Tell::Home
        );
        assert_eq!(pick_tell(&inp("home", Cond::Well, false), None), Tell::Home);
    }

    #[test]
    fn the_mute_gate_is_the_devices() {
        // Never a fault message => mute, whatever condition says (ring.py:38).
        assert_eq!(device_condition(false, Some("hurts")), Cond::Mute);
        assert_eq!(device_condition(false, Some("well")), Cond::Mute);
        assert_eq!(device_condition(false, None), Cond::Mute);
        // A fault arrived: the word rules, and silence of the word is "well"
        // (ring.py:40).
        assert_eq!(device_condition(true, Some("hurts")), Cond::Hurts);
        assert_eq!(device_condition(true, Some("niggle")), Cond::Niggle);
        assert_eq!(device_condition(true, None), Cond::Well);
    }

    #[test]
    fn breaths_are_the_reference_curves() {
        // golden (animations.py:59-68 at fps=30): home = 90+24+126+15 frames,
        // busy = 72+15+102+12.
        assert_eq!(home_curve().len(), 255);
        assert_eq!(busy_curve().len(), 201);
        // The endpoints: base 3.0 in, peak 35.0 at hold, base out.
        assert_eq!(home_curve()[0], BASE);
        assert_eq!(home_curve()[89 + 24], HOME_PEAK); // into "hold"
        assert_eq!(home_curve()[254], BASE);
        assert_eq!(busy_curve()[72 + 15], BUSY_PEAK);
    }

    #[test]
    fn home_hue_drifts_green_to_cyan() {
        // animations.py:116-118; golden: 120.0 at 0, 165.0 at 30, back down.
        assert!((home_hue(0.0) - 120.0).abs() < 1e-12);
        assert!((home_hue(30.0) - 165.0).abs() < 1e-9);
        assert!((home_hue(15.0) - 142.5).abs() < 1e-9);
        assert!((home_hue(71.3) - 133.9985822965845).abs() < 1e-9);
    }
}
