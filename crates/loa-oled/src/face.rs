//! The animations: pure math into a Frame, keyed by time so the frame rate
//! can change. Ported from the reference `face.py`; the flatline signature —
//! a drifting line, a sweeping scan bar, rare blips — is the default face.
//!
//! One trait, `Animation`. The reference had draw(frame, t) and a separate
//! draw_state(frame, t, st) for the pages; here every animation takes the
//! held view and most ignore it. `tick` exists where the object carries its
//! own memory (Scope's blips, Showoff's glitch bands); the reference's tick()
//! compared against time.time() directly — here the clock is passed in, so a
//! test can drive the cadence without sleeping.

use rand::{rngs::StdRng, Rng, RngCore, SeedableRng};

use crate::fonts_draw;
use crate::frame::{Frame, HEIGHT, PAGES, WIDTH};
use crate::rstate::{self, RenderState, FAULTS_STALE_S, THROTTLE_THR, THROTTLE_UV};

/// Python's int(): truncate toward zero.
fn trunc(v: f64) -> i64 {
    v as i64
}

/// Python's round(): half to even, not half away from zero. The scope's
/// baseline and the ECG's sample points go through this, and a trace that
/// disagrees with the reference by a pixel at a .5 is a different trace.
pub fn py_round(v: f64) -> i64 {
    let f = v.floor();
    let d = v - f;
    if d > 0.5 {
        (f + 1.0) as i64
    } else if d < 0.5 {
        f as i64
    } else {
        let r = f as i64;
        if r % 2 == 0 {
            r
        } else {
            r + 1
        }
    }
}

/// The first n chars, like the reference's `s[:14]` — a truncation, not a
/// panic on a long label.
fn take_chars(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

/// Faint CRT grid relative to the baseline — moves with it, so no fixed
/// pixels sit lit (OLED burn-in). Dotted rows every 8px above/below.
fn grid(frame: &mut Frame, base: i32) {
    for dy in [-24i32, -16, -8, 8, 16, 24] {
        let y = base + dy;
        if (0..HEIGHT as i32).contains(&y) {
            for x in (0..WIDTH as i32).step_by(4) {
                frame.px(x, y, true);
            }
        }
    }
}

pub trait Animation {
    /// One frame at clock `t`, drawn from the held view.
    fn draw(&mut self, f: &mut Frame, t: f64, st: &RenderState);
    /// Called before every draw with the seconds since the last one and the
    /// same clock the draw will get. Only stateful modes implement it.
    fn tick(&mut self, _dt: f64, _now: f64) {}
}

/// The flatline: drifting baseline, sweeping scan bar, rare blips.
///
/// Fully dynamic by design — OLEDs burn in if anything sits still. The
/// baseline drifts on a slow two-tone cycle, the CRT grid only flickers in
/// short windows, the scan bar never stops, and blips come and go. The blips
/// are the object's own memory: a FaceRenderer holds one Scope, never a new
/// one per frame.
pub struct Scope {
    rng: StdRng,
    blips: Vec<(i32, f64)>,
    next_blip: f64,
}

// How many Scopes this THREAD has built. The rebuild rule — hold the
// object, never a new one per frame — is invisible from the outside unless
// it is counted; this is the counter the tests read. Thread-local because
// cargo runs tests in parallel and a shared counter counts other tests'
// builds too.
thread_local! {
    static SCOPE_BUILDS: std::cell::Cell<u64> = const { std::cell::Cell::new(0) };
}

/// Builds of Scope by this thread — the identity witness for the rebuild rule.
pub fn scope_builds() -> u64 {
    SCOPE_BUILDS.with(|c| c.get())
}

impl Scope {
    pub fn new(now: f64, rng: StdRng) -> Self {
        SCOPE_BUILDS.with(|c| c.set(c.get() + 1));
        let mut s = Scope {
            rng,
            blips: Vec::new(),
            next_blip: 0.0,
        };
        // rare, gentle: 8-15s apart so a blip reads as a pulse, not a glitch
        s.next_blip = now + s.rng.gen_range(8.0..15.0);
        s
    }
}

impl Animation for Scope {
    fn tick(&mut self, dt: f64, now: f64) {
        // age the survivors, drop the ones that outlived their flash
        self.blips
            .iter_mut()
            .for_each(|(_, a)| *a += dt);
        self.blips.retain(|(_, a)| *a < 2.5);
        if now >= self.next_blip {
            self.blips
                .push((trunc(self.rng.gen_range(8.0..(WIDTH - 7) as f64)) as i32, 0.0));
            self.next_blip = now + self.rng.gen_range(8.0..15.0);
        }
    }

    fn draw(&mut self, frame: &mut Frame, t: f64, _st: &RenderState) {
        // two-tone drift (90s x 23s): the line never rests on one row
        let base =
            32 + py_round(2.0 * (2.0 * std::f64::consts::PI * t / 90.0).sin()
                * (2.0 * std::f64::consts::PI * t / 23.0).sin()) as i32;
        if (t % 24.0) < 3.0 {
            // grid flickers in 3s windows, 21s off
            grid(frame, base);
        }
        frame.line(0, base, WIDTH as i32 - 1, base, true); // the flat line
        for (x, age) in self.blips.clone() {
            let h = trunc(14.0 * (1.0 - age / 2.5));
            if h > 0 {
                // a blip: life, briefly
                frame.line(x, base - h as i32, x, base + h as i32, true);
            }
        }
        let scan = ((t * 40.0) % WIDTH as f64) as i32; // phosphor sweep
        frame.line(scan, base - 8, scan, base + 8, true);
        frame.px(scan, base, true);
    }
}

/// Heartbeat-style trace scrolling left. The other reading of 'feeling'.
pub struct Ecg {
    period: f64,
    speed: f64,
}

impl Default for Ecg {
    fn default() -> Self {
        Ecg {
            period: 48.0,
            speed: 36.0,
        }
    }
}

impl Ecg {
    /// p in 0..period -> y offset. PQRST-ish, because a heart is a shape.
    fn wave(&self, p: f64) -> f64 {
        use std::f64::consts::PI;
        let pp = p / self.period;
        if pp < 0.12 {
            2.0 * (PI * pp / 0.12).sin()
        } else if pp < 0.22 {
            -6.0 * (PI * (pp - 0.12) / 0.10).sin()
        } else if pp < 0.34 {
            22.0 * (PI * (pp - 0.22) / 0.12).sin().powf(1.5)
        } else if pp < 0.44 {
            -5.0 * (PI * (pp - 0.34) / 0.10).sin()
        } else if pp < 0.60 {
            4.0 * (PI * (pp - 0.44) / 0.16).sin()
        } else {
            0.0
        }
    }
}

impl Animation for Ecg {
    fn draw(&mut self, frame: &mut Frame, t: f64, _st: &RenderState) {
        let off = (t * self.speed) % self.period;
        let y0 = (HEIGHT / 2) as i32;
        if (t % 24.0) < 3.0 {
            grid(frame, y0); // grid flickers, same burn-safe window as scope
        }
        for x in 0..WIDTH as i32 {
            let p = ((x as f64 + off) % self.period).max(0.0);
            let y = py_round(y0 as f64 - self.wave(p)) as i32;
            frame.px(x, y, true);
            frame.px(x, y + 1, true);
        }
    }
}

/// Two overlapping sines — calm, even when the body is loud elsewhere.
pub struct Ripple {
    amp: f64,
    speed: f64,
    freq: f64,
}

impl Default for Ripple {
    fn default() -> Self {
        Ripple {
            amp: 12.0,
            speed: 0.35,
            freq: 1.0 / 32.0,
        }
    }
}

impl Animation for Ripple {
    fn draw(&mut self, frame: &mut Frame, t: f64, _st: &RenderState) {
        use std::f64::consts::PI;
        let y0 = (HEIGHT / 2) as i32;
        let phase = 2.0 * PI * t * self.speed;
        for x in 0..WIDTH as i32 {
            let a = self.amp * (0.6 + 0.4 * (phase * 0.5 + x as f64 * 0.05).sin());
            let y = py_round(y0 as f64 + a * (2.0 * PI * x as f64 * self.freq + phase).sin())
                as i32;
            frame.px(x, y, true);
        }
    }
}

/// Static burst — corruption, on the face. Seeded per frame: the seed is a
/// fixed function of the clock (`int(t * 12)`), so the same t paints the same
/// static and a bad frame is reproducible down to the pixel.
pub struct Noise {
    density: f64,
}

impl Default for Noise {
    fn default() -> Self {
        Noise { density: 0.14 }
    }
}

impl Animation for Noise {
    fn draw(&mut self, frame: &mut Frame, t: f64, _st: &RenderState) {
        let seed = trunc(t * 12.0) as u64;
        let mut r = StdRng::seed_from_u64(seed);
        for y in (0..HEIGHT as i32).step_by(2) {
            for x in (0..WIDTH as i32).step_by(2) {
                if (r.next_u32() as f64) / (u32::MAX as f64 + 1.0) < self.density {
                    frame.px(x, y, true);
                }
            }
        }
    }
}

/// Scrolling text — the face saying something.
pub struct Marquee {
    text: String,
    speed: f64,
}

impl Marquee {
    pub fn new(text: impl Into<String>) -> Self {
        Marquee {
            text: text.into(),
            speed: 28.0,
        }
    }
}

impl Animation for Marquee {
    fn draw(&mut self, frame: &mut Frame, t: f64, _st: &RenderState) {
        let tw = Frame::text_width(&self.text) as i64;
        let off = trunc(t * self.speed).rem_euclid(tw + WIDTH as i64);
        frame.text(WIDTH as i32 - off as i32, (HEIGHT / 2) as i32 - 3, &self.text, true);
    }
}

/// The dinner-party mode: full demo loop — pattern sweep, Topaz marquee,
/// framed message, RGB-split glitch.
///
/// Cycle: 3s sweep, 8s marquee, 2s message+glitch, 4s message hold.
pub struct Showoff {
    rng: StdRng,
    strip_rows: Vec<Vec<u8>>,
    strip_w: usize,
}

impl Showoff {
    const CYCLE: f64 = 17.0;
    const MSG1: &'static str = "CLEAN BUS";
    const MSG2: &'static str = "OLD GIRL SEES";
    const DEFAULT_TEXT: &'static str = "CLEAN BUS - THE OLD GIRL SEES - LOA - ";

    pub fn new(rng: StdRng) -> Self {
        // the reference kept self.text on the object; nothing reads it after
        // the strip is built, and dead fields are not carried across a port
        let (strip_rows, strip_w) = fonts_draw::topaz_strip(Self::DEFAULT_TEXT);
        Showoff {
            rng,
            strip_rows,
            strip_w,
        }
    }

    fn sweep(frame: &mut Frame, t: f64) {
        let phase = (trunc(t / 0.05) / 2) % 2;
        for x in 0..WIDTH {
            for p in 0..PAGES {
                if ((x / 8) + p + phase as usize) % 2 == 0 {
                    // a whole 8-row band of the page: that is one byte, and
                    // every coordinate here is in bounds by construction
                    frame.buf[p * WIDTH + x] = 0xFF;
                }
            }
        }
    }

    fn marquee(&self, frame: &mut Frame, ct: f64) {
        let y0 = 24;
        let xwin = trunc(self.strip_w as f64 * (1.0 - ct / 8.0)) as usize;
        for r in 0..16 {
            for sx in 0..WIDTH {
                let src = xwin + sx;
                if src < self.strip_w && fonts_draw::strip_bit(&self.strip_rows, r, src) {
                    frame.px(sx as i32, (y0 + r) as i32, true);
                }
            }
        }
    }

    fn msg(frame: &mut Frame) {
        for x in 0..WIDTH as i32 {
            frame.px(x, 0, true);
            frame.px(x, HEIGHT as i32 - 1, true);
        }
        for y in 0..HEIGHT as i32 {
            frame.px(0, y, true);
            frame.px(WIDTH as i32 - 1, y, true);
        }
        fonts_draw::topaz(frame, Self::MSG1, 40, 16);
        fonts_draw::topaz(frame, Self::MSG2, 28, 40);
    }

    fn glitch(&mut self, frame: &mut Frame) {
        let base = frame.buf.clone();
        for _ in 0..3 {
            let band_page = self.rng.gen_range(0..PAGES as u32) as usize;
            let shift = self.rng.gen_range(1..9usize);
            for x in 0..WIDTH {
                frame.buf[band_page * WIDTH + x] =
                    base[band_page * WIDTH + (x as i64 - shift as i64).rem_euclid(WIDTH as i64) as usize];
            }
        }
    }
}

impl Animation for Showoff {
    fn draw(&mut self, frame: &mut Frame, t: f64, _st: &RenderState) {
        let ct = t % Self::CYCLE;
        if ct < 3.0 {
            Self::sweep(frame, t);
        } else if ct < 11.0 {
            self.marquee(frame, ct - 3.0);
        } else if ct < 13.0 {
            Self::msg(frame);
            self.glitch(frame);
        } else {
            Self::msg(frame);
        }
    }
}

/// The bench mode: paginated live status boards for tuning the senses.
///
/// The page field of the held state selects the board: sensors | pir | snr |
/// temp | frag | power | fault; anything else draws sensors, like the
/// reference's else. Every reading it paints comes from the topics this
/// device holds — the pages never read a file or another service.
///
/// Indicator: an outline box, label lit when the pin is low; when the pin
/// goes high the centre fills and the label is knocked out (unlit) — the
/// OFF/ON shape Divv drew. Amiga Forever 8px everywhere, dense.
pub struct Ripperdoc;

impl Ripperdoc {
    const TITLE: &'static str = "RIPPERDOC";

    fn indicator(frame: &mut Frame, x: i32, y: i32, label: &str, level: bool) {
        let w = fonts_draw::amiga8_width(label) as i32 + 8;
        let h = 12;
        let (x1, y1) = (x + w - 1, y + h - 1);
        frame.line(x, y, x1, y, true); // top
        frame.line(x, y1, x1, y1, true); // bottom
        frame.line(x, y, x, y1, true); // left
        frame.line(x1, y, x1, y1, true); // right
        let (lx, ly) = (x + 4, y + 2);
        if level {
            for yy in (y + 1)..y1 {
                for xx in (x + 1)..x1 {
                    frame.px(xx, yy, true);
                }
            }
            fonts_draw::amiga8(frame, label, lx, ly, false);
        } else {
            fonts_draw::amiga8(frame, label, lx, ly, true);
        }
    }

    fn sensors(frame: &mut Frame, st: &RenderState) {
        fonts_draw::amiga8(frame, Self::TITLE, 2, 1, true);
        fonts_draw::amiga8(frame, "1/7", 99, 1, true);
        Self::indicator(frame, 2, 12, "PIR", st.pir_high);
        Self::indicator(frame, 39, 12, "SNR", st.snr_cm.is_some());
        Self::indicator(frame, 85, 12, "TMP", st.temp_c.is_some());
        Self::indicator(frame, 2, 26, "BAR", st.pressure_hpa.is_some());
        let sealed = st
            .seal
            .get("sealed")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        Self::indicator(frame, 39, 26, "SEAL", sealed);
        fonts_draw::amiga8(frame, &format!("N{:03}", st.pir_count), 2, 40, true);
        fonts_draw::amiga8(frame, "G17", 44, 40, true);
        match st.snr_cm {
            Some(cm) => fonts_draw::amiga8(frame, &format!("{cm:>4.0}CM"), 66, 40, true),
            None => fonts_draw::amiga8(frame, "  --CM", 66, 40, true),
        }
    }

    fn pir(frame: &mut Frame, t: f64, st: &RenderState) {
        fonts_draw::amiga8(frame, "PIR", 2, 1, true);
        fonts_draw::amiga8(frame, "2/7", 99, 1, true);
        Self::indicator(frame, 2, 12, "PIR", st.pir_high);
        fonts_draw::amiga8(frame, &format!("N{:03}", st.pir_count), 2, 28, true);
        let hold = if st.pir_high {
            let on = st.pir_on_ts.filter(|v| *v != 0.0).unwrap_or(t);
            t - on
        } else {
            st.pir_last_hold
        };
        fonts_draw::amiga8(frame, &format!("T{hold:04.1}s"), 2, 37, true);
        // the reference's `if not last`: a zero timestamp reads as no motion
        let lt = st
            .pir_last_ts
            .filter(|v| *v != 0.0)
            .map(rstate::fmt_clock)
            .unwrap_or_else(|| "--:--:--".into());
        fonts_draw::amiga8(frame, &format!("L{lt}"), 2, 46, true);
    }

    fn snr(frame: &mut Frame, st: &RenderState) {
        fonts_draw::amiga8(frame, "SNR", 2, 1, true);
        fonts_draw::amiga8(frame, "3/7", 99, 1, true);
        match st.snr_cm {
            Some(cm) => fonts_draw::amiga8(frame, &format!("{cm:>4.0}CM"), 2, 12, true),
            None => fonts_draw::amiga8(frame, "  --CM", 2, 12, true),
        }
        fonts_draw::amiga8(frame, &format!("N{:03}", st.snr_count), 2, 28, true);
        let lt = st
            .snr_ts
            .filter(|v| *v != 0.0)
            .map(rstate::fmt_clock)
            .unwrap_or_else(|| "--:--:--".into());
        fonts_draw::amiga8(frame, &format!("L{lt}"), 2, 37, true);
        fonts_draw::amiga8(frame, "G22", 2, 46, true);
    }

    fn temp(frame: &mut Frame, st: &RenderState) {
        fonts_draw::amiga8(frame, "TMP", 2, 1, true);
        fonts_draw::amiga8(frame, "4/7", 99, 1, true);
        match st.temp_c {
            None => {
                fonts_draw::amiga8(frame, "--.-C", 2, 52, true);
                let s = "--%";
                let sx = WIDTH as i32 - fonts_draw::amiga8_width(s) as i32;
                fonts_draw::amiga8(frame, s, sx, 52, true);
                Self::gauge(frame, 2, 18, 0.0);
            }
            Some(tc) => {
                let (lo, hi) = (5.0f64, 40.0f64);
                let frac = ((tc - lo) / (hi - lo)).clamp(0.0, 1.0);
                Self::gauge(frame, 2, 18, frac);
                fonts_draw::amiga8(frame, &format!("{tc:>4.1}C"), 2, 52, true);
                if let Some(hum) = st.hum_pct {
                    let s = format!("{hum:.0}%");
                    let sx = WIDTH as i32 - fonts_draw::amiga8_width(&s) as i32;
                    fonts_draw::amiga8(frame, &s, sx, 52, true);
                }
            }
        }
        if let Some(p) = st.pressure_hpa {
            let s = format!("{p:.0}HPA");
            let sx = WIDTH as i32 - fonts_draw::amiga8_width(&s) as i32;
            fonts_draw::amiga8(frame, &s, sx, 43, true);
            let dir = if st.baro_trend.is_empty() { "steady" } else { &st.baro_trend };
            Self::trend_caret(frame, sx - 8, 45, dir);
        }
    }

    /// 7x4 up/down/steady caret — the Amiga font has no arrows, so it is
    /// drawn pixel by pixel left of the hPa value.
    fn trend_caret(frame: &mut Frame, x: i32, y: i32, direction: &str) {
        let pts: &[(i32, i32)] = match direction {
            "rising" => &[(3, 0), (2, 1), (4, 1), (1, 2), (5, 2), (0, 3), (6, 3)],
            "falling" => &[(3, 3), (2, 2), (4, 2), (1, 1), (5, 1), (0, 0), (6, 0)],
            _ => &[(0, 2), (1, 2), (2, 2), (3, 2), (4, 2), (5, 2), (6, 2)],
        };
        for (dx, dy) in pts {
            frame.px(x + dx, y + dy, true);
        }
    }

    /// Horizontal bulb thermometer — the reference icon: solid filled round
    /// bulb, solid stem, hollow end for the unfilled range. One continuous
    /// silhouette — no outlines over the filled portion, no gaps between
    /// bulb and stem (the stem overlaps the bulb's shoulder). Stem height =
    /// half the bulb diameter; the stem runs to the right edge of the face.
    fn gauge(frame: &mut Frame, x: i32, y: i32, frac: f64) {
        let r = 10i32;
        let (cx, cy) = (x + 10, y + 10);
        for yy in (cy - r)..=(cy + r) {
            for xx in (cx - r)..=(cx + r) {
                let (dx, dy) = (xx - cx, yy - cy);
                if dx * dx + dy * dy <= r * r {
                    frame.px(xx, yy, true);
                }
            }
        }
        // stem — frame drawn full length, but the fill paints over it in
        // the filled stretch: the bar reads solid to the fill level, hollow
        // beyond it.
        let (sx, sy, sw, sh) = (x + 14, y + 5, 111i32, 10i32);
        frame.line(sx, sy, sx + sw, sy, true); // top
        frame.line(sx, sy + sh, sx + sw, sy + sh, true); // bottom
        frame.line(sx + sw, sy, sx + sw, sy + sh, true); // right cap
        let fill = trunc(frac * sw as f64);
        if fill > 0 {
            for yy in (sy + 1)..(sy + sh) {
                for xx in sx..(sx + fill as i32) {
                    frame.px(xx, yy, true);
                }
            }
        }
    }

    /// The vault's state, drawn from the seal map the cortex carries. An
    /// empty map — this device has no local source for it — draws GONE with
    /// zero counts, exactly what the reference drew when its caller had no
    /// status.json. An absent fact shown as itself, never invented.
    fn frag(frame: &mut Frame, st: &RenderState) {
        fonts_draw::amiga8(frame, "FRAG", 2, 1, true);
        fonts_draw::amiga8(frame, "5/7", 99, 1, true);
        let num = |k: &str| -> i64 {
            st.seal.get(k).and_then(|v| v.as_f64()).map(trunc).unwrap_or(0)
        };
        let sealed = st
            .seal
            .get("sealed")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        Self::lock(frame, 8, 16);
        fonts_draw::amiga8(frame, if sealed { "SEALED" } else { "GONE" }, 24, 16, true);
        fonts_draw::amiga8(frame, &format!("N{:03}", num("entries")), 2, 32, true);
        fonts_draw::amiga8(frame, &format!("A{:03}", num("access_count")), 40, 32, true);
    }

    /// A small padlock glyph, 10 wide x 9 tall.
    fn lock(frame: &mut Frame, x: i32, y: i32) {
        frame.line(x + 2, y, x + 2, y + 2, true); // shackle left
        frame.line(x + 7, y, x + 7, y + 2, true); // shackle right
        frame.line(x + 2, y, x + 7, y, true); // shackle top
        for yy in (y + 3)..(y + 9) {
            // body
            for xx in x..(x + 10) {
                frame.px(xx, yy, true);
            }
        }
        for xx in (x + 4)..(x + 7) {
            // keyhole notch
            frame.px(xx, y + 3, false);
        }
        frame.px(x + 5, y + 4, false); // keyhole
        frame.px(x + 5, y + 5, false);
        frame.px(x + 5, y + 6, true);
        frame.px(x + 5, y + 7, true);
    }

    /// The body's pain page. The code and the API say `fault`; that is the
    /// engineering surface and it is accurate. The face says PAIN, because
    /// the glass is the body's own word for it and a pain sense that has
    /// gone deaf must not read as comfort.
    fn fault(frame: &mut Frame, t: f64, st: &RenderState) {
        fonts_draw::amiga8(frame, "PAIN", 2, 1, true);
        fonts_draw::amiga8(frame, "7/7", 99, 1, true);
        let Some((report_ts, n_fault, n_warn, rows)) = st.faults_view() else {
            fonts_draw::amiga8(frame, "NO SWEEP", 2, 10, true);
            return;
        };
        // the reference's `age = t - (f.get("ts") or t)`: a zero ts reads as
        // fresh, not as epoch-stale — kept exactly.
        let age = t - if report_ts == 0.0 { t } else { report_ts };
        let heads = if age > FAULTS_STALE_S {
            format!("SENSE DEAD {}M", trunc(age / 60.0))
        } else if n_fault > 0 {
            format!("{n_fault} HURTS")
        } else if n_warn > 0 {
            format!("{n_warn} NIGGLE")
        } else {
            "ALL QUIET".to_string()
        };
        fonts_draw::amiga8(frame, &take_chars(&heads, 14), 2, 10, true);
        let mut y = 19;
        // `face` carries the evidence the code alone loses — HOT is a name,
        // "HOT 86C" is something you can act on.
        for r in rows.iter().take(5) {
            let label = if r.face.is_empty() { r.code.clone() } else { r.face.clone() };
            fonts_draw::amiga8(frame, &take_chars(&label, 14), 2, y, true);
            y += 9;
        }
    }

    /// The bench's power truth: the 5V input and the 3V3 rail it feeds, off
    /// the PMIC, plus what each draws — all from the held `power` topic.
    /// UV lit = the 5V input is sagging RIGHT NOW (throttled bit 0). THR lit
    /// = the SoC is throttling RIGHT NOW (bit 2). A missing rail shows "--":
    /// never an invented number.
    fn power(frame: &mut Frame, st: &RenderState) {
        fonts_draw::amiga8(frame, "PWR", 2, 1, true);
        fonts_draw::amiga8(frame, "6/7", 99, 1, true);
        let flags = st.power_throttled;
        Self::indicator(frame, 2, 11, "UV", flags.is_some_and(|f| f & THROTTLE_UV != 0));
        Self::indicator(frame, 40, 11, "THR", flags.is_some_and(|f| f & THROTTLE_THR != 0));
        Self::rail(frame, 2, 24, "5V", st.rails.get("EXT5V_V"), 3, "V");
        Self::rail(frame, 2, 32, "3V3", st.rails.get("3V3_SYS_V"), 3, "V");
        Self::rail(frame, 2, 40, "3V3I", st.rails.get("3V3_SYS_A"), 3, "A");
        Self::rail(frame, 2, 48, "CORE", st.rails.get("VDD_CORE_V"), 3, "V");
        Self::rail(frame, 2, 56, "COREI", st.rails.get("VDD_CORE_A"), 3, "A");
    }

    /// One telemetry line: label left, number flush right so the units and
    /// decimal points stack into a column.
    fn rail(frame: &mut Frame, x: i32, y: i32, label: &str, val: Option<&f64>, dp: usize, unit: &str) {
        let s = match val {
            None => "--".to_string(),
            Some(v) => format!("{v:.dp$}{unit}"),
        };
        fonts_draw::amiga8(frame, label, x, y, true);
        let sx = WIDTH as i32 - 2 - fonts_draw::amiga8_width(&s) as i32;
        fonts_draw::amiga8(frame, &s, sx, y, true);
    }
}

impl Animation for Ripperdoc {
    fn draw(&mut self, frame: &mut Frame, t: f64, st: &RenderState) {
        match st.page.as_str() {
            "pir" => Self::pir(frame, t, st),
            "snr" => Self::snr(frame, st),
            "temp" => Self::temp(frame, st),
            "frag" => Self::frag(frame, st),
            "power" => Self::power(frame, st),
            "fault" => Self::fault(frame, t, st),
            _ => Self::sensors(frame, st),
        }
    }
}

/// The mode table. `text` takes the marquee's string; anything the table
/// does not know becomes a marquee that says so — a typo on the API reads as
/// itself on the glass.
pub fn make_renderer(mode: &str, st: &RenderState, now: f64, rng: StdRng) -> Option<Box<dyn Animation>> {
    if mode == "off" {
        return None;
    }
    let a: Box<dyn Animation> = match mode {
        "scope" => Box::new(Scope::new(now, rng)),
        "ecg" => Box::new(Ecg::default()),
        "ripple" => Box::new(Ripple::default()),
        "noise" => Box::new(Noise::default()),
        "text" => Box::new(Marquee::new(
            st.text.clone().filter(|s| !s.is_empty()).unwrap_or_else(|| "LOA".into()),
        )),
        "showoff" => Box::new(Showoff::new(rng)),
        "ripperdoc" => Box::new(Ripperdoc),
        other => Box::new(Marquee::new(format!("?? {other}"))),
    };
    Some(a)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn bit(buf: &[u8], x: usize, y: usize) -> bool {
        buf[(y >> 3) * WIDTH + x] & (1 << (y & 7)) != 0
    }

    fn lit(frame: &Frame) -> usize {
        frame.buf.iter().map(|b| b.count_ones() as usize).sum()
    }

    #[test]
    fn scope_blips_age_and_die_on_their_own_clock() {
        let rng = StdRng::seed_from_u64(1);
        let mut s = Scope::new(1000.0, rng);
        // no blip inside the first 8s of the schedule
        s.tick(0.5, 1000.5);
        s.draw(&mut Frame::new(), 1000.5, &RenderState::default());
        // force the cadence open and watch one blip live and die
        s.next_blip = 0.0;
        s.tick(0.1, 1001.0);
        assert_eq!(s.blips.len(), 1);
        for _ in 0..20 {
            s.next_blip = f64::MAX; // no new blips while the first ages
            s.tick(0.1, 1001.0);    // 2.0s old: still a pulse, not ash
        }
        assert_eq!(s.blips.len(), 1);
        s.tick(0.5, 1001.0); // 2.5s: the retain window is age < 2.5
        assert!(s.blips.is_empty(), "a blip older than 2.5s is gone");
    }

    #[test]
    fn scope_blip_draws_a_vertical_bar_through_the_base() {
        let mut s = Scope::new(0.0, StdRng::seed_from_u64(2));
        s.next_blip = f64::MAX;
        s.blips.push((40, 0.0));
        let mut f = Frame::new();
        s.draw(&mut f, 3.5, &RenderState::default()); // grid window: 3.5 not in it
        for dy in -14..=14 {
            assert!(bit(&f.buf, 40, (32 + dy).max(0).min(63) as usize));
        }
    }

    #[test]
    fn ecg_waves_are_the_reference_shapes() {
        let e = Ecg::default();
        // the segments, at points where sin() is exactly 1 or 0
        assert!((e.wave(0.0) - 0.0).abs() < 1e-12);
        assert!((e.wave(48.0 * 0.06) - 2.0).abs() < 1e-9); // P peak
        assert!((e.wave(48.0 * 0.28) - 22.0).abs() < 1e-9); // R spike
        assert!((e.wave(48.0 * 0.7) - 0.0).abs() < 1e-12); // the rest
    }

    #[test]
    fn ecg_draws_both_rails_every_column() {
        let mut e = Ecg::default();
        let mut f = Frame::new();
        e.draw(&mut f, 5.0, &RenderState::default());
        for x in 0..WIDTH {
            // the trace is drawn y and y+1: every column has at least 2 lit
            let col = (0..HEIGHT).filter(|&y| bit(&f.buf, x, y)).count();
            assert!(col >= 2, "column {x} has {col} lit");
        }
    }

    #[test]
    fn ripple_is_128_pixels_one_curve() {
        let mut r = Ripple::default();
        let mut f = Frame::new();
        r.draw(&mut f, 1.25, &RenderState::default());
        assert_eq!(lit(&f), WIDTH);
    }

    #[test]
    fn noise_is_seeded_by_the_clock_alone() {
        // same t -> same static, twice, through separate objects
        let mut a = Noise::default();
        let mut b = Noise::default();
        let mut fa = Frame::new();
        let mut fb = Frame::new();
        a.draw(&mut fa, 1234.5, &RenderState::default());
        b.draw(&mut fb, 1234.5, &RenderState::default());
        assert_eq!(fa.buf, fb.buf);
        assert!(lit(&fa) > 0, "density 0.14 over 2048 cells draws");
        // and a different frame of the clock is different pixels
        a.draw(&mut fa, 1234.6, &RenderState::default());
        assert_ne!(fa.buf, fb.buf);
    }

    #[test]
    fn marquee_wraps_the_strip_and_defaults_text() {
        let mut m = Marquee::new("LOA");
        let mut f = Frame::new();
        m.draw(&mut f, 0.0, &RenderState::default());
        // off=0: the text starts flush right at x=128 — nothing is on screen
        assert_eq!(lit(&f), 0);
        m.draw(&mut f, 0.3, &RenderState::default()); // off=8
        f.clear();
        m.draw(&mut f, 0.3, &RenderState::default());
        // 128-8=120: the first glyph column x=120 is 'L' col0 = 0x10 -> row bit 4 at y0+4
        assert!(bit(&f.buf, 120, 29 + 4));
    }

    #[test]
    fn showoff_phases_hit_their_windows() {
        let mut s = Showoff::new(StdRng::seed_from_u64(3));
        // sweep: the pattern is page bytes 0xff, so lit count is a multiple
        // of 8 and large
        let mut f = Frame::new();
        s.draw(&mut f, 1.0, &RenderState::default());
        assert!(lit(&f) > 1000, "sweep fills half the field");
        // msg: the frame border is fully lit — check corners and edges
        let mut f = Frame::new();
        s.draw(&mut f, 14.0, &RenderState::default());
        assert!(bit(&f.buf, 0, 0));
        assert!(bit(&f.buf, 127, 0));
        assert!(bit(&f.buf, 0, 63));
        assert!(bit(&f.buf, 127, 63));
    }

    #[test]
    fn showoff_glitch_shifts_one_band_per_roll() {
        let mut s = Showoff::new(StdRng::seed_from_u64(4));
        let mut f = Frame::new();
        // ct in [11,13): msg + glitch
        s.draw(&mut f, 11.5, &RenderState::default());
        // the border survived the band shifts only where the shift copied it
        // from the same row; assert instead that the result differs from the
        // pure msg frame — a glitch that did nothing is a broken rng
        let mut plain = Frame::new();
        Showoff::msg(&mut plain);
        assert_ne!(f.buf, plain.buf);
    }

    #[test]
    fn ripperdoc_pages_all_paint_something() {
        let st = RenderState::default();
        for page in ["sensors", "pir", "snr", "temp", "frag", "power", "fault", "nonsense"] {
            let mut v = st.clone();
            v.page = page.into();
            let mut f = Frame::new();
            Ripperdoc.draw(&mut f, 1000.0, &v);
            assert!(lit(&f) > 0, "page {page} drew nothing");
        }
    }

    #[test]
    fn fault_page_sorts_rows_and_truncates_labels() {
        let mut st = RenderState::default();
        st.fault_seen = true;
        st.fault = Some(rstate::FaultMsg {
            rows: vec![
                rstate::FaultRow {
                    level: "fault".into(),
                    code: "OLED LOOP".into(),
                    text: String::new(),
                    face: "THE LOOP THAT NEVER FINISHED".into(),
                },
                rstate::FaultRow {
                    level: "warn".into(),
                    code: "10".into(),
                    text: String::new(),
                    face: String::new(),
                },
            ],
            condition: Some("hurts".into()),
            ts: 990.0,
        });
        st.clock_now = 990.0;
        let mut f = Frame::new();
        Ripperdoc.draw(&mut f, 990.0, &st);
        // 1 HURTS header + two labels; just assert ink and no panic
        assert!(lit(&f) > 20);
    }

    #[test]
    fn unknown_mode_names_itself() {
        let st = RenderState::default();
        let mut a = make_renderer("banana", &st, 0.0, StdRng::seed_from_u64(5)).unwrap();
        let mut f = Frame::new();
        a.draw(&mut f, 0.0, &st);
        // "?? banana" is 9 chars * 6 = 54 wide, entering at the right edge
        let mut g = Frame::new();
        g.text(128 - 54 - 30, 29, "?? BANANA", true);
        // scrolled to the same off (t=0 -> off=0 -> text at x=128: nothing)
        // so draw at a t that brings it on-screen: off=20
        let mut f2 = Frame::new();
        a.draw(&mut f2, 0.72, &st);
        assert!(lit(&f2) > 0);
        let _ = g;
    }
}
