//! The wire's view, assembled for the panes.
//!
//! ONE source: the topics. There is no HTTP read path here and there is not
//! going to be one — commands go over POST /api and nothing a pane draws is
//! ever fetched. The Python console has a tombstone for the same reason.
//!
//! Merging is by PRESENCE, like the cortex: a reading carries only what its
//! daemon measured, so a motion event cannot wipe the mood.

use loa_topic::pb;

#[derive(Default, Clone)]
pub struct View {
    /// 1024 B, 1bpp, page-major — the panel as the body drove it.
    pub face: Vec<u8>,
    /// 72 B, 24 px RGB.
    pub ring: Vec<u8>,
    pub page: String,
    pub mood: String,
    pub oled_mode: String,
    pub ring_state: String,
    pub condition: String,
    pub oled_flip: bool,
    pub oled_dim: bool,
    pub snr_cm: Option<f64>,
    pub pir_high: bool,
    pub pir_count: u32,
    pub pir_last_hold: f64,
    pub temp_c: Option<f64>,
    pub hum_pct: Option<f64>,
    pub pressure_hpa: Option<f64>,
    pub baro_temp_c: Option<f64>,
    pub baro_trend: String,
    pub faults: Vec<(String, String, String)>,
    /// 3V3_SYS_V and the throttle bitfield, from the rails map.
    pub v3: Option<f64>,
    pub throttle: Option<f64>,
    pub last_frame_at: f64,
    pub frames: u64,
}

impl View {
    pub fn apply(&mut self, topic: &str, env: &pb::Envelope) {
        use pb::envelope::Body;
        let Some(body) = env.body.as_ref() else { return };
        match body {
            Body::Ripperdoc(m) => {
                if let Some(f) = m.face.as_ref() {
                    if f.len() == 1024 {
                        self.face.clone_from(f);
                        self.frames += 1;
                        self.last_frame_at = now();
                    }
                }
                if let Some(v) = m.page.as_ref() {
                    self.page.clone_from(v);
                }
                if let Some(v) = m.mood.as_ref() {
                    self.mood.clone_from(v);
                }
                if let Some(v) = m.oled_mode.as_ref() {
                    self.oled_mode.clone_from(v);
                }
                if let Some(v) = m.ring_state.as_ref() {
                    self.ring_state.clone_from(v);
                }
                if let Some(v) = m.condition.as_ref() {
                    self.condition.clone_from(v);
                }
                if let Some(v) = m.oled_flip {
                    self.oled_flip = v;
                }
                if let Some(v) = m.oled_dim {
                    self.oled_dim = v;
                }
            }
            Body::Ring(m) => {
                if let Some(r) = m.ring.as_ref() {
                    if r.len() == 72 {
                        self.ring.clone_from(r);
                    }
                }
            }
            Body::Pir(m) => {
                if let Some(v) = m.high {
                    self.pir_high = v;
                }
                if let Some(v) = m.count {
                    self.pir_count = v;
                }
                if let Some(v) = m.last_hold {
                    self.pir_last_hold = v;
                }
            }
            Body::Sonar(m) => {
                self.snr_cm = m.cm;
            }
            Body::Baro(m) => {
                self.pressure_hpa = m.pressure_hpa;
                self.baro_temp_c = m.temp_c;
                if let Some(t) = m.trend.as_ref() {
                    self.baro_trend.clone_from(t);
                }
            }
            Body::Weather(m) => {
                self.temp_c = m.temp_c;
                self.hum_pct = m.hum_pct;
            }
            Body::Power(m) => {
                self.v3 = m.rails.get("3V3_SYS_V").copied();
                self.throttle = m.rails.get("throttled").copied();
            }
            Body::Fault(m) => {
                if let Some(c) = m.condition.as_ref() {
                    self.condition.clone_from(c);
                }
                self.faults = m
                    .rows
                    .iter()
                    .map(|r| {
                        (
                            r.level.clone(),
                            r.code.clone(),
                            r.text.clone(),
                        )
                    })
                    .collect();
            }
            _ => {}
        }
        let _ = topic;
    }

    /// One line, the console's own words: what the body is doing, and how old
    /// the picture is. A pane that shows its last frame forever is the ghost
    /// temperature with a different coat on.
    pub fn status(&self, age_s: f64) -> String {
        if age_s > 5.0 {
            return format!("  NO FEED — ALL QUIET ({age_s:.0}s silent)");
        }
        let pwr = match self.v3 {
            None => "PWR --".to_string(),
            Some(v) => {
                let mut s = format!("3V3 {v:.2}V");
                match throttle_bits(self.throttle) {
                    Some(b) if b & 0x1 != 0 => s.push_str(" UV!"),
                    Some(b) if b & 0x4 != 0 => s.push_str(" THR!"),
                    _ => {}
                }
                s
            }
        };
        format!(
            " mood {:<8} ring {:<6} oled {:<9} page {:<7} PIR {:<5} SNR {:<3} N{:04} T{:5.1}s {}",
            or_q(&self.mood),
            or_q(&self.ring_state),
            or_q(&self.oled_mode),
            or_q(&self.page),
            if self.pir_high { "SOLID" } else { "open" },
            if self.snr_cm.is_some() { "ON" } else { "OFF" },
            self.pir_count,
            self.pir_last_hold,
            pwr
        )
    }
}

fn or_q(s: &str) -> &str {
    if s.is_empty() {
        "?"
    } else {
        s
    }
}

/// `throttled` rides the `rails` map, typed `double` on the wire, so the
/// bitfield arrives as 983040.0. The bits are the bits; read them as ints.
/// (The same trap the Python console hit on its second tick, found live.)
pub fn throttle_bits(value: Option<f64>) -> Option<u64> {
    value.map(|v| v as u64)
}

pub fn now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// 1024 bytes, page-major, 1bpp -> 32 rows of 128 half-block cells.
///
/// ONE CELL IS 1x2 PIXELS: no scaling, the live IS the panel. A console that
/// scales is a console that lies about the glass.
pub fn face_art(buf: &[u8]) -> Vec<String> {
    let mut out = Vec::with_capacity(32);
    for row in 0..32 {
        let y = row as u32 * 2;
        let mut line = String::with_capacity(128);
        for x in 0..128usize {
            let top = px(buf, x, y);
            let bot = px(buf, x, y + 1);
            line.push(match (top, bot) {
                (true, true) => '█',
                (true, false) => '▀',
                (false, true) => '▄',
                (false, false) => ' ',
            });
        }
        out.push(line);
    }
    out
}

fn px(buf: &[u8], x: usize, y: u32) -> bool {
    let idx = (y as usize / 8) * 128 + x;
    buf.get(idx).map(|b| b & (1 << (y % 8)) != 0).unwrap_or(false)
}
