//! The renderer: clock state + error diffusion -> 72 code-space bytes.
//! Carries `RingRenderer` from `loa/cortex/ring.py:86-196` and
//! `DitheredFrame` from `loa/ring/encode.py:31-53` (READ ONLY references).
//!
//! No tick rides the wire — state is news, frames are news — so the
//! animation clock is the daemon's own `Instant`. Every finished frame is
//! news too: written to the strip AND pushed up on inbound topic "ring".

use std::collections::HashSet;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use rand::rngs::StdRng;
use rand::{Rng as _, SeedableRng as _};

use crate::encode::BYTES;
use crate::tells::{self, Cond, RingInput, Tell};

/// DitheredFrame (encode.py:31-40): per-LED per-channel error accumulators
/// in CODE space. One instance per tell — "create one per state so no
/// fractional carryover flashes when the ring changes state" (encode.py:34-35),
/// enforced where the tell changes (ring.py:127-129).
#[derive(Clone)]
pub struct Dither {
    acc: Vec<[f64; 3]>,
}

impl Dither {
    pub fn new() -> Self {
        Dither {
            acc: vec![[0.0; 3]; 24],
        }
    }

    /// _blit (ring.py:187-196, same maths as encode.py:42-53): add the float
    /// frame into the accumulator, emit the integer part, keep the remainder.
    /// Python's int() truncates toward zero and the clamp is on EMIT only —
    /// the accumulator keeps its sign, which is what lets a rise borrow.
    pub fn render(&mut self, frame: &[(f64, f64, f64)]) -> [u8; BYTES] {
        let mut raw = [0u8; BYTES];
        for i in 0..frame.len().min(24) {
            let chans = [frame[i].0, frame[i].1, frame[i].2];
            for (ch, val) in chans.iter().enumerate() {
                self.acc[i][ch] += val;
                let emitted = self.acc[i][ch] as i64;
                self.acc[i][ch] -= emitted as f64;
                raw[i * 3 + ch] = emitted.clamp(0, 255) as u8;
            }
        }
        raw
    }
}

impl Default for Dither {
    fn default() -> Self {
        Self::new()
    }
}

/// One render step: the tell that spoke, and its 72 bytes (RGB, r,g,b per
/// LED — GRB is the wire's business, the encoder owns that).
pub struct Frame {
    pub tell: Tell,
    pub rgb: [u8; BYTES],
}

/// A held one-shot record (ring.py:97): kind + when it started on OUR clock.
/// A record, not a flag: it clears only when its own sequence finishes
/// (ring.py:171-181) — a GATED record (state not home) does not expire.
struct OneShot {
    kind: Tell, // Scan or Glitch
    start: f64,
}

/// The ring's frame builder: all state that persists across renders.
pub struct Renderer {
    clock: Instant,
    // Sequences built once at FPS=30, like the module-level lists in
    // ring.py:72-77. Home/busy store the brightness curve; the hue is
    // applied at sample time (home_hue, ring.py:162-163; busy is fixed 45).
    home: Vec<f64>,
    busy: Vec<f64>,
    scan_slow: Vec<Vec<(f64, f64, f64)>>, // (1.6, 0.5) — ring.py:177
    scan_fast: Vec<Vec<(f64, f64, f64)>>, // (0.9, 0.3)
    glitch_len: usize,                    // frames in the built glitch shape
    rng: StdRng,
    tell: Option<Tell>,
    dither: Dither,
    oneshot: Option<OneShot>,
    /// Dedupe of event records by (state word, ts): a feed replay of the
    /// same note must not re-fire a scan. The f64 rides as its bit pattern
    /// — the same bits are the same ts, and Eq for floats is exactly what
    /// a dedupe key needs.
    seen: HashSet<(String, u64)>,
    /// niggle schedule (ring.py:98-99): next start time, current tick end.
    niggle_next: f64,
    niggle_until: f64,
}

impl Default for Renderer {
    fn default() -> Self {
        Self::new()
    }
}

impl Renderer {
    pub fn new() -> Self {
        Renderer {
            clock: Instant::now(),
            home: tells::home_curve(),
            busy: tells::busy_curve(),
            scan_slow: tells::scan_frames(1.6, 0.5),
            scan_fast: tells::scan_frames(0.9, 0.3),
            glitch_len: tells::glitch_frames(|| true).len(),
            rng: StdRng::seed_from_u64(wall_now().to_bits()),
            tell: None,
            dither: Dither::new(),
            oneshot: None,
            seen: HashSet::new(),
            niggle_next: 0.0,
            niggle_until: 0.0,
        }
    }

    /// The daemon's animation clock in seconds (ring.py:109 samples
    /// time.time(); the internal clock is the device's own — no tick is
    /// received, the frames it produces are the news).
    pub fn t(&self) -> f64 {
        self.clock.elapsed().as_secs_f64()
    }

    /// note_oneshot (ring.py:101-106) plus (kind, ts) dedupe: an event with
    /// kind "ring" and detail.state in scan|glitch is a one-shot record; the
    /// NEWEST waits to play (ring.py:106 replaces, never queues).
    pub fn note_oneshot(&mut self, state_word: &str, ts: f64) {
        if state_word != "scan" && state_word != "glitch" {
            return; // ring.py:105: other kinds are not records
        }
        if !self.seen.insert((state_word.to_string(), ts.to_bits())) {
            return; // dedupe by (kind, ts)
        }
        let t = self.t();
        let kind = if state_word == "glitch" {
            Tell::Glitch
        } else {
            Tell::Scan
        };
        self.oneshot = Some(OneShot { kind, start: t });
    }

    pub fn pending_oneshot(&self) -> Option<Tell> {
        self.oneshot.as_ref().map(|o| o.kind)
    }

    /// The current decision without rendering (for tests and the loop's
    /// logging): the ladder plus the held record.
    pub fn tell_now(&self, input: &RingInput) -> Tell {
        tells::pick_tell(input, self.pending_oneshot())
    }

    /// render (ring.py:108-130): decide, build the float frame, reset the
    /// dither when the tell CHANGED, blit through the error diffusion.
    pub fn render(&mut self, input: &RingInput) -> Frame {
        let kind = self.tell_now(input);
        let t = self.t();
        // A gated record does not expire (doctrine): while a higher tier or
        // a non-home state holds the ring, the held record's start rides
        // with the clock, so it plays its FULL sequence when the gate lifts.
        // (Python's start is the event's wall ts and ages during gating;
        // this is the one place the device clock improves on the letter.)
        if let Some(os) = &mut self.oneshot {
            if kind != Tell::Scan && kind != Tell::Glitch {
                os.start = t;
            }
        }
        let frame = match kind {
            Tell::Alarm => self.alarm(t),
            Tell::Hurts => self.hurts(t),
            Tell::Mute => self.mute(t),
            Tell::Busy => self.busy(t),
            Tell::Scan => self.scan(t, input).unwrap_or_else(|| self.home(t, input)),
            Tell::Glitch => self.glitch(t).unwrap_or_else(|| self.home(t, input)),
            Tell::Home => self.home(t, input),
        };
        // The one-shot branches may have SPENT the record (ring.py:172,180);
        // what spoke this frame is then Home, and the reset check sees it.
        let spoke = if (kind == Tell::Scan || kind == Tell::Glitch) && self.oneshot.is_none() {
            Tell::Home
        } else {
            kind
        };
        if Some(spoke) != self.tell {
            // ring.py:127-129: tell changed -> fresh DitheredFrame.
            self.tell = Some(spoke);
            self.dither = Dither::new();
        }
        Frame {
            tell: spoke,
            rgb: self.dither.render(&frame),
        }
    }

    /// _alarm (ring.py:134-138): 0.4s on / 0.3s off x3, then a 2.0s rest.
    fn alarm(&self, t: f64) -> Vec<(f64, f64, f64)> {
        let p = t % tells::ALARM_CYCLE_S;
        if p >= 3.0 * 0.7 {
            return tells::solid(0.0, 0.0, 0.0); // the rest at the end (ring.py:136-137)
        }
        if (p % 0.7) < 0.4 {
            tells::solid(0.0, 1.0, 1.0) // full red (ring.py:138)
        } else {
            tells::solid(0.0, 0.0, 0.0)
        }
    }

    /// _hurts (ring.py:140-142): seven steps of 0.28s through _HURTS, hue 0,
    /// s 1.0 — deep red, held. Deliberately not the alarm flash (ring.py:57-59).
    fn hurts(&self, t: f64) -> Vec<(f64, f64, f64)> {
        let i = ((t % tells::HURTS_S) / tells::HURTS_STEP_S) as usize;
        let v = tells::HURTS[i.min(tells::HURTS.len() - 1)]; // ring.py:142 min clamp
        tells::solid(0.0, 1.0, v)
    }

    /// _mute (ring.py:144-146): dark, one dim (v=0.06) blink in the last
    /// 0.4s of each 10s — alive but cannot speak; without the blink a deaf
    /// body and an unplugged cable look identical (ring.py:67-68).
    fn mute(&self, t: f64) -> Vec<(f64, f64, f64)> {
        let in_blink = (t % tells::MUTE_S) >= (tells::MUTE_S - tells::BLINK_S);
        let v = if in_blink { 0.06 } else { 0.0 };
        tells::solid(0.0, 0.0, v) // s=0: the blink is white, not red (ring.py:145)
    }

    /// _busy (ring.py:148-149): the amber breath sampled off the clock.
    fn busy(&self, t: f64) -> Vec<(f64, f64, f64)> {
        let v = tells::sample_scalar(&self.busy, t, tells::BUSY_S);
        tells::solid(45.0, 1.0, v / 255.0) // hue 45 (animations.py:124)
    }

    /// _home (ring.py:151-163): the green breath with its 60s hue drift; a
    /// niggle rides the breath as one rare amber tick instead of replacing
    /// it (ring.py:152-153).
    fn home(&mut self, t: f64, input: &RingInput) -> Vec<(f64, f64, f64)> {
        if input.cond == Cond::Niggle {
            if t >= self.niggle_next {
                self.niggle_next = t + tells::NIGGLE_PERIOD_S; // ring.py:156
                self.niggle_until = t + 0.36; // ring.py:157
            }
            if t < self.niggle_until {
                // ring.py:159: bright 0.55 for the first half, dim 0.10 for the second.
                let bright = t < (self.niggle_until - 0.18);
                let v = if bright { 0.55 } else { 0.10 };
                return tells::solid(38.0, 1.0, v); // amber (ring.py:160)
            }
        }
        self.breath(t)
    }

    /// The breath itself (ring.py:161-163): sample _HOME_BREATH, re-hue.
    /// Python pulls the scalar out of the frame with max(p)/255; the curve
    /// IS the scalar here, so the two are the same number.
    fn breath(&self, t: f64) -> Vec<(f64, f64, f64)> {
        let level = tells::sample_scalar(&self.home, t, tells::HOME_S);
        let hue = tells::home_hue(t);
        vec![tells::hsv(hue, 1.0, level / 255.0); 24]
    }

    /// The scan branch of _oneshot (ring.py:176-183): timings switch on the
    /// console flag; elapsed past lap+fade spends the record (None -> the
    /// caller falls through to home, which is what ring.py's None return does).
    fn scan(&mut self, t: f64, input: &RingInput) -> Option<Vec<(f64, f64, f64)>> {
        let os = self.oneshot.as_ref()?;
        let (lap, fade) = tells::scan_timings(input.ripperdoc); // ring.py:177
        let total = lap + fade;
        let elapsed = t - os.start;
        if elapsed > total {
            self.oneshot = None; // ring.py:180
            return None;
        }
        let seq = if input.ripperdoc {
            &self.scan_fast
        } else {
            &self.scan_slow
        };
        Some(tells::sample_frames(seq, elapsed, total))
    }

    /// The glitch branch of _oneshot (ring.py:170-175). Each stutter frame
    /// draws fresh red/cyan coins (animations.py:110 — the RNG is per-render
    /// in Python too; only the shape is deterministic), and the last frame
    /// of the sequence is dark (animations.py:112).
    fn glitch(&mut self, t: f64) -> Option<Vec<(f64, f64, f64)>> {
        let os = self.oneshot.as_ref()?;
        let elapsed = t - os.start;
        if elapsed > tells::GLITCH_S {
            self.oneshot = None; // ring.py:172
            return None;
        }
        // ring.py:82 sample: int(elapsed / total * len)
        let i = (elapsed / tells::GLITCH_S * self.glitch_len as f64) as usize;
        let i = i.min(self.glitch_len - 1);
        if i >= self.glitch_len - 1 {
            return Some(vec![(0.0, 0.0, 0.0); 24]); // the dark tail frame
        }
        Some(
            (0..24)
                .map(|_| {
                    if self.rng.gen_bool(0.5) {
                        (255.0, 0.0, 0.0)
                    } else {
                        (0.0, 255.0, 255.0)
                    }
                })
                .collect(),
        )
    }

    // Test seams: the caches are private; tests check lengths through these.
    #[cfg(test)]
    pub fn seq_lens(&self) -> (usize, usize, usize, usize) {
        (
            self.home.len(),
            self.busy.len(),
            self.scan_slow.len(),
            self.scan_fast.len(),
        )
    }

    #[cfg(test)]
    pub fn glitch_len(&self) -> usize {
        self.glitch_len
    }
}

/// Wall seconds — frame stamps on the wire, and the rng seed. The animation
/// clock is not this; it is `Renderer::t`.
pub fn wall_now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn input(rs: &str, cond: Cond, rd: bool) -> RingInput {
        RingInput {
            cond,
            ring_state: rs.into(),
            ripperdoc: rd,
        }
    }

    #[test]
    fn dither_synthesizes_between_codes() {
        // encode.py's whole point at the low end: a steady 0.5 per channel
        // emits 0,1,0,1 (golden dither_half from the Python DitheredFrame).
        let mut d = Dither::new();
        let frame = vec![(0.5, 0.0, 0.0); 24];
        assert_eq!(d.render(&frame)[0], 0);
        assert_eq!(d.render(&frame)[0], 1);
        assert_eq!(d.render(&frame)[0], 0);
        assert_eq!(d.render(&frame)[0], 1);
    }

    #[test]
    fn dither_matches_python_home_ramp() {
        // Golden dither_home_px0 (the Python's own _blit over the first 255
        // home frames): px0 channel emissions at frames 0,1,45,89,112.
        let mut d = Dither::new();
        let curve = tells::home_curve();
        let mut got = Vec::new();
        for i in 0..255 {
            let v = curve[i];
            // The Python frame at hue 120 is (0, v, 0); feed that.
            got.push(d.render(&vec![(0.0, v, 0.0); 24])[..3].to_vec());
        }
        assert_eq!(got[0], vec![0, 3, 0]);
        assert_eq!(got[1], vec![0, 3, 0]);
        assert_eq!(got[2], vec![0, 3, 0]);
        assert_eq!(got[45], vec![0, 22, 0]);
        assert_eq!(got[89], vec![0, 35, 0]);
        assert_eq!(got[112], vec![0, 35, 0]);
    }

    #[test]
    fn dither_resets_on_kind_change() {
        // ring.py:127-129. Home accumulates a fractional debt; the first
        // busy frame after the change must NOT inherit it: fresh dither of
        // the exact busy opener hsv(45,1,3/255) = (3.0, 2.25, 0) emits
        // (3,2,0). The same first render on a renderer that had been home
        // is what this asserts.
        let mut r = Renderer::new();
        let _ = r.render(&input("home", Cond::Well, false));
        let _ = r.render(&input("home", Cond::Well, false));
        let f = r.render(&input("busy", Cond::Well, false));
        assert_eq!(f.tell, Tell::Busy);
        assert_eq!(&f.rgb[..3], &[3, 2, 0]);
    }

    #[test]
    fn one_shot_plays_once_only_at_home_and_gated_does_not_expire() {
        let mut r = Renderer::new();
        r.note_oneshot("scan", 1000.0);
        assert_eq!(r.pending_oneshot(), Some(Tell::Scan));
        // Gated: the state word is not "home" -> no one-shot plays, and the
        // RECORD survives (ring.py:166 returns None without clearing).
        let f = r.render(&input("scan", Cond::Well, false));
        assert_eq!(f.tell, Tell::Home);
        assert_eq!(r.pending_oneshot(), Some(Tell::Scan));
        // Dedupe: same (kind, ts) again is a no-op; a newer ts replaces.
        r.note_oneshot("scan", 1000.0);
        assert_eq!(r.pending_oneshot(), Some(Tell::Scan));
        r.note_oneshot("glitch", 1001.0);
        assert_eq!(r.pending_oneshot(), Some(Tell::Glitch));
        // At home it plays: first render is a glitch.
        assert_eq!(
            r.render(&input("home", Cond::Well, false)).tell,
            Tell::Glitch
        );
    }

    #[test]
    fn glitch_record_spends_itself_after_03s() {
        // ring.py:170-175: elapsed past 0.3s clears the record; the frame
        // after that is home. 0.31s is a cheap wait.
        let mut r = Renderer::new();
        r.note_oneshot("glitch", 42.0);
        assert_eq!(
            r.render(&input("home", Cond::Well, false)).tell,
            Tell::Glitch
        );
        std::thread::sleep(std::time::Duration::from_millis(310));
        assert_eq!(r.render(&input("home", Cond::Well, false)).tell, Tell::Home);
        assert_eq!(r.pending_oneshot(), None); // spent, once only
    }

    #[test]
    fn scan_record_spends_itself_after_the_lap() {
        // ring.py:176-181: slow timings total 1.6+0.5 = 2.1s.
        let mut r = Renderer::new();
        r.note_oneshot("scan", 7.0);
        assert_eq!(r.render(&input("home", Cond::Well, false)).tell, Tell::Scan);
        std::thread::sleep(std::time::Duration::from_millis(2110));
        assert_eq!(r.render(&input("home", Cond::Well, false)).tell, Tell::Home);
        assert_eq!(r.pending_oneshot(), None);
    }

    #[test]
    fn the_scan_sequence_caches_match_the_goldens() {
        // ring.py:177 both branches, animations.py at FPS=30:
        // (1.6,0.5) -> 48+15 = 63; (0.9,0.3) -> 27+9 = 36. Glitch 9+1 = 10.
        let r = Renderer::new();
        let (home, busy, slow, fast) = r.seq_lens();
        assert_eq!((home, busy, slow, fast), (255, 201, 63, 36));
        assert_eq!(r.glitch_len(), 10);
    }

    #[test]
    fn scan_timings_switch_changes_the_frames() {
        // One clock, one held record, two renders at the same elapsed: the
        // ripperdoc flag picks the cache. At elapsed ~0 the head is at LED 0
        // — pixel 0 lit (hsv(150,0.7,120/255) = (36,120,78), golden
        // h150s7b), pixels 1..5 dark (the tail wraps into 19..23).
        let mut r = Renderer::new();
        r.note_oneshot("scan", 0.0);
        let f0 = r.render(&input("home", Cond::Well, true));
        assert_eq!(f0.tell, Tell::Scan);
        assert!(
            f0.rgb[1] > 100,
            "head LED green must be ~120: {:?}",
            &f0.rgb[..3]
        );
        assert_eq!(&f0.rgb[3..15], &[0u8; 12]);
        // Half a second in, the console sequence (lap 0.9 + fade 0.3) is at
        // frame 15 of 36 — head ~13.3, pixels 8..13 lit. The slow sequence
        // (1.6 + 0.5) is at frame 15 of 63 — head ~7.5, pixels 2..7 lit.
        // The same elapsed must land on visibly different frames.
        std::thread::sleep(std::time::Duration::from_millis(500));
        let fast = r.render(&input("home", Cond::Well, true));
        let slow = r.render(&input("home", Cond::Well, false));
        assert_eq!(fast.tell, Tell::Scan);
        assert_eq!(slow.tell, Tell::Scan);
        assert_ne!(
            fast.rgb, slow.rgb,
            "same elapsed must land on different frames"
        );
    }

    #[test]
    fn non_ring_kinds_are_not_records() {
        let mut r = Renderer::new();
        r.note_oneshot("baro", 1.0);
        assert_eq!(r.pending_oneshot(), None);
        r.note_oneshot("scan", 2.0);
        r.note_oneshot("motion", 3.0); // still the scan
        assert_eq!(r.pending_oneshot(), Some(Tell::Scan));
    }

    #[test]
    fn mute_is_dark_with_one_white_blink_per_ten_seconds() {
        // golden mute_blink: hsv(0,0,0.06) = (15.3,15.3,15.3) — white, not
        // red, because s=0 (ring.py:145).
        let px = tells::hsv(0.0, 0.0, 0.06);
        assert!((px.0 - 15.3).abs() < 1e-9);
        assert!((px.1 - 15.3).abs() < 1e-9);
        assert!((px.2 - 15.3).abs() < 1e-9);
        // The blink window through the mute frame builder at t = 9.85:
        let r = Renderer::new();
        let f = r.mute(9.85); // t inside the last 0.4s of the 10s period
        assert!(f[0].0 > 0.0);
        let d = r.mute(5.0); // mid-period: dark
        assert_eq!(d[0], (0.0, 0.0, 0.0));
    }

    #[test]
    fn hurts_ladder_is_the_golden_deep_red() {
        // golden hurts_hi/lo through _solid(0,1,v): hsv(0,1,0.85)=(216.75,0,0),
        // hsv(0,1,0.18)=(45.9,0,0).
        let r = Renderer::new();
        let hi = r.hurts(0.0); // t=0 -> step 0 -> 0.85
        assert!((hi[0].0 - 216.75).abs() < 1e-9);
        assert!((hi[0].1 - 0.0).abs() < 1e-12);
        let lo = r.hurts(0.28 * 3.0 + 0.01); // step 3 -> 0.18
        assert!((lo[0].0 - 45.9).abs() < 1e-9);
        let last = r.hurts(tells::HURTS_S - 0.001); // step 6, min clamp holds at i=6
        assert!((last[0].0 - 45.9).abs() < 1e-9);
    }

    #[test]
    fn alarm_beats_hurts_end_to_end() {
        let mut r = Renderer::new();
        let f = r.render(&input("alarm", Cond::Hurts, false));
        assert_eq!(f.tell, Tell::Alarm);
    }

    #[test]
    fn niggle_ticks_ride_the_breath_on_a_20s_period() {
        // ring.py:154-160: first niggle render fires the tick (bright half),
        // and the tick lasts 0.36s, then the breath returns.
        let mut r = Renderer::new();
        let niggle = input("home", Cond::Niggle, false);
        let f1 = r.home(r.t(), &niggle);
        assert_eq!(f1[0], tells::hsv(38.0, 1.0, 0.55)); // amber, bright half
                                                        // golden niggle_hi: (140.25, 88.825, 0)
        assert!((f1[0].0 - 140.25).abs() < 1e-9);
        std::thread::sleep(std::time::Duration::from_millis(370));
        let f2 = r.home(r.t(), &niggle);
        // Past the tick: back on the breath (green hue ~120, not amber 38).
        assert!(f2[0].2 > f2[0].0); // blue above red on a green-cyan hue
                                    // And the next tick is scheduled ~20s out, not ~0.4s out.
        assert!(r.niggle_next > r.t() + 19.0);
    }
}
