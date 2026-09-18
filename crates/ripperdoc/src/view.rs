//! What the screen is made of: the model the feed fills, and the panes it
//! fills.
//!
//! ONE source: the topics. There is no HTTP read path here and there is not
//! going to be one — commands go over POST /api and nothing a pane draws is
//! ever fetched. The Python console has a tombstone for the same reason.
//!
//! Merging is by PRESENCE, like the cortex: a message carries only what its
//! publisher knows, so a pir event cannot wipe the mood.

use loa_wire::pb;
use ratatui::layout::{Constraint, Direction, Layout};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span, Text};
use ratatui::widgets::{Block, Borders, Paragraph};
use ratatui::Frame;

pub const BLUE: Color = Color::Rgb(0x0F, 0x0F, 0xAF);
pub const WHITE: Color = Color::Rgb(0xFF, 0xFF, 0xFF);
pub const ORANGE: Color = Color::Rgb(0xFF, 0x99, 0x00);
pub const BACKDROP: Color = Color::Rgb(0x0A, 0x0E, 0x12);
pub const DIM: Color = Color::Rgb(0x23, 0x2C, 0x38);

/// The page set, in key order: `1` is sensors, `7` is fault. Same seven the
/// body renders, same order the cortex validates against.
pub const PAGES: [&str; 7] = ["sensors", "pir", "snr", "temp", "frag", "power", "fault"];

pub const MENU: &str = "  r ripperdoc   1..7 pages   m mood   f flip   q quit";

/// The last thing the feed said, field by field.
#[derive(Default, Clone)]
pub struct Model {
    /// 1024 B, 1bpp, page-major — the panel as the body drove it.
    pub face: Vec<u8>,
    /// 72 B, 24 px RGB.
    pub ring: Vec<u8>,
    pub page: String,
    pub mood: String,
    pub oled_mode: String,
    pub ring_state: String,
    pub condition: String,
    pub oled_text: String,
    pub oled_flip: bool,
    pub oled_dim: bool,
    pub snr_on: bool,
    pub ripperdoc: bool,
    pub snr_cm: Option<f64>,
    pub snr_count: Option<u32>,
    pub pir_high: bool,
    pub pir_count: u32,
    pub pir_last_hold: f64,
    pub temp_c: Option<f64>,
    pub hum_pct: Option<f64>,
    pub pressure_hpa: Option<f64>,
    pub baro_trend: String,
    pub faults: Vec<(String, String, String)>,
    /// The last event off the wire, as one line: kind and its detail.
    pub event: String,
    /// 3V3_SYS and the throttle bitfield, from the rails map.
    pub v3: Option<f64>,
    pub throttle: Option<f64>,
    /// Count of face frames SEEN. The draw loop watches this for liveness and
    /// for the feed's own frames/s — the console's redraw rate is its own.
    pub frames: u64,
}

impl Model {
    /// Fold one decoded message in. Every field lands only when present: the
    /// feed is news, and news carries what changed.
    pub fn apply(&mut self, topic: &str, env: &pb::Envelope) {
        use pb::envelope::Body;
        let Some(body) = env.body.as_ref() else {
            return;
        };
        match body {
            Body::Ripperdoc(m) => {
                if let Some(f) = m.face.as_ref() {
                    if f.len() == 1024 {
                        self.face.clone_from(f);
                        self.frames += 1;
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
                if let Some(v) = m.oled_text.as_ref() {
                    self.oled_text.clone_from(v);
                }
                if let Some(v) = m.oled_flip {
                    self.oled_flip = v;
                }
                if let Some(v) = m.oled_dim {
                    self.oled_dim = v;
                }
                if let Some(v) = m.snr_on {
                    self.snr_on = v;
                }
                if let Some(v) = m.ripperdoc {
                    self.ripperdoc = v;
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
                self.snr_count = m.count;
            }
            Body::Weather(m) => {
                self.temp_c = m.temp_c;
                self.hum_pct = m.hum_pct;
                // baro folded here: pressure and its trend ride the weather
                // message, presence-checked like everything else.
                if let Some(p) = m.pressure_hpa {
                    self.pressure_hpa = Some(p);
                }
                if let Some(t) = m.trend.as_ref() {
                    self.baro_trend.clone_from(t);
                }
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
                    .map(|r| (r.level.clone(), r.code.clone(), r.text.clone()))
                    .collect();
            }
            Body::Event(m) => {
                self.event = fmt_event(m);
            }
            // state and init never arrive on these subscriptions, and if the
            // topic list ever grows this arm keeps compiling honestly.
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
            if self.snr_on { "ON" } else { "OFF" },
            self.pir_count,
            self.pir_last_hold,
            pwr
        )
    }
}

/// The event line: kind first, its detail sorted so the same event draws the
/// same words twice. A map iterates in whatever order it likes; the log pane
/// should not flicker over key order.
fn fmt_event(m: &pb::Event) -> String {
    if m.detail.is_empty() {
        return m.kind.clone();
    }
    let mut keys: Vec<&String> = m.detail.keys().collect();
    keys.sort();
    let det: Vec<String> = keys
        .iter()
        .map(|k| format!("{k}={}", m.detail[*k]))
        .collect();
    format!("{} {}", m.kind, det.join(" "))
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
    buf.get(idx)
        .map(|b| b & (1 << (y % 8)) != 0)
        .unwrap_or(false)
}

// ---------------------------------------------------------------------------
// The draw. One pass, four rows: menu, status, the live pair, the log.
// ---------------------------------------------------------------------------

pub fn draw(f: &mut Frame, v: &Model, age: f64, fps: f64, feed_fps: f64) {
    let area = f.area();
    let rows = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(1), // menubar
            Constraint::Length(3), // status
            Constraint::Min(32),   // live: face + ring
            Constraint::Min(0),    // log
        ])
        .split(area);

    f.render_widget(
        Paragraph::new(MENU).style(Style::default().bg(BLUE).fg(WHITE)),
        rows[0],
    );

    // The status line carries the counters at its tail: what this program
    // redraws at, and what the feed actually sends. They are different
    // numbers and showing only one of them is a half-lie.
    let mut status = v.status(age);
    status.push_str(&format!("   {fps:.0} fps   feed {feed_fps:.0} f/s"));
    f.render_widget(
        Paragraph::new(status)
            .style(Style::default().bg(Color::Black).fg(WHITE))
            .block(
                Block::default()
                    .borders(Borders::ALL)
                    .border_style(Style::default().fg(BLUE)),
            ),
        rows[1],
    );

    let live = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([Constraint::Length(130), Constraint::Min(28)])
        .split(rows[2]);

    draw_face(f, v, age, live[0]);
    draw_ring(f, v, live[1]);
    draw_log(f, v, rows[3]);
}

/// FACE — the panel's own bytes, one cell per 1x2 pixels, no scaling.
fn draw_face(f: &mut Frame, v: &Model, age: f64, area: ratatui::layout::Rect) {
    let art: Text = if v.face.len() == 1024 && age < 5.0 {
        face_art(&v.face)
            .into_iter()
            .map(Line::from)
            .collect::<Vec<_>>()
            .into()
    } else {
        Text::from(if age >= 5.0 {
            "NO FEED — the panel has not arrived"
        } else {
            "waiting for a frame"
        })
    };
    f.render_widget(
        Paragraph::new(art).style(Style::default().fg(WHITE)).block(
            Block::default()
                .title("FACE")
                .borders(Borders::ALL)
                .border_style(Style::default().fg(BLUE)),
        ),
        area,
    );
}

/// RING — 24 px of RGB, straight off the wire, drawn as the ring itself.
fn draw_ring(f: &mut Frame, v: &Model, area: ratatui::layout::Rect) {
    let leds: Vec<Span> = (0..24)
        .map(|i| {
            let (r, g, b) = if v.ring.len() >= 72 {
                (v.ring[i * 3], v.ring[i * 3 + 1], v.ring[i * 3 + 2])
            } else {
                (0, 0, 0)
            };
            Span::styled(
                "  ",
                Style::default().bg(if (r as u16 + g as u16 + b as u16) < 12 {
                    DIM
                } else {
                    Color::Rgb(r, g, b)
                }),
            )
        })
        .collect();
    let mut lines = vec![Line::from(leds), Line::from(""), {
        let flip = if v.oled_flip { " FLIP" } else { "" };
        let dim = if v.oled_dim { " DIM" } else { "" };
        Line::from(format!("state  {}{flip}{dim}", or_q(&v.ring_state)))
    }];
    if !v.oled_text.is_empty() {
        lines.push(Line::from(Span::styled(
            truncate(&v.oled_text, 24),
            Style::default().add_modifier(Modifier::BOLD),
        )));
    }
    if v.ring.len() < 72 {
        lines.push(Line::from("RING OFFLINE"));
    }
    f.render_widget(
        Paragraph::new(Text::from(lines))
            .style(Style::default().fg(WHITE))
            .block(
                Block::default()
                    .title("RING")
                    .borders(Borders::ALL)
                    .border_style(Style::default().fg(BLUE)),
            ),
        area,
    );
}

/// The log pane: the body's condition, its fault rows, the last event, and
/// the sensor lines the status row has no room for.
fn draw_log(f: &mut Frame, v: &Model, area: ratatui::layout::Rect) {
    let cond = or_q(&v.condition);
    let mut lines = vec![Line::from(format!(
        "condition {cond}   faults {}   frames {}   flip {}   dim {}",
        v.faults.len(),
        v.frames,
        v.oled_flip,
        v.oled_dim
    ))];
    if !v.event.is_empty() {
        lines.push(Line::from(Span::styled(
            format!("  event   {}", v.event),
            Style::default().fg(ORANGE),
        )));
    }
    for (level, code, text) in v.faults.iter().take(6) {
        lines.push(Line::from(Span::styled(
            format!("  {level:<5} {code:<12} {text}"),
            Style::default().fg(if level == "fault" { ORANGE } else { WHITE }),
        )));
    }
    if let Some(cm) = v.snr_cm {
        lines.push(Line::from(format!("  sonar   {cm:.1} cm")));
    }
    if let (Some(t), Some(h)) = (v.temp_c, v.hum_pct) {
        lines.push(Line::from(format!("  weather {t:.1}C {h:.0}%")));
    }
    if let Some(p) = v.pressure_hpa {
        lines.push(Line::from(format!(
            "  baro    {p:.1} hPa {}",
            or_q(&v.baro_trend)
        )));
    }
    f.render_widget(
        Paragraph::new(Text::from(lines))
            .style(Style::default().bg(BACKDROP).fg(WHITE))
            .block(
                Block::default()
                    .title("LOG")
                    .borders(Borders::ALL)
                    .border_style(Style::default().fg(DIM)),
            ),
        area,
    );
}

fn truncate(s: &str, n: usize) -> String {
    if s.chars().count() <= n {
        s.to_string()
    } else {
        let mut out: String = s.chars().take(n.saturating_sub(1)).collect();
        out.push('…');
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_page_set_is_the_seven_the_cortex_validates() {
        assert_eq!(
            PAGES,
            ["sensors", "pir", "snr", "temp", "frag", "power", "fault"]
        );
    }

    #[test]
    fn a_blank_frame_is_blanks_and_a_set_bit_lands_where_the_byte_says() {
        let blank = face_art(&[0u8; 1024]);
        assert_eq!(blank.len(), 32);
        assert!(blank.iter().all(|l| l.trim().is_empty()));

        // page-major: byte 0 is column 0, rows 0..8; row 1 is the SECOND bit.
        let mut buf = [0u8; 1024];
        buf[0] = 0b0000_0010; // y=1 set, y=0 clear -> lower half only
        let art = face_art(&buf);
        assert!(art[0].starts_with('▄'));
    }

    #[test]
    fn presence_merge_cannot_wipe_unsent_fields() {
        let mut m = Model::default();
        let rip = pb::envelope::Body::Ripperdoc(pb::Ripperdoc {
            mood: Some("busy".into()),
            ..Default::default()
        });
        m.apply(
            "ripperdoc",
            &pb::Envelope {
                schema_version: 10,
                body: Some(rip),
            },
        );
        let ring = pb::envelope::Body::Ripperdoc(pb::Ripperdoc {
            page: Some("snr".into()),
            ..Default::default()
        });
        m.apply(
            "ripperdoc",
            &pb::Envelope {
                schema_version: 10,
                body: Some(ring),
            },
        );
        assert_eq!(m.mood, "busy", "a message without mood must not clear it");
        assert_eq!(m.page, "snr");
    }

    #[test]
    fn the_throttle_bitfield_survives_the_double() {
        assert_eq!(throttle_bits(Some(983040.0)), Some(983040));
        assert_eq!(throttle_bits(None), None);
    }

    #[test]
    fn fault_rows_land_level_code_text() {
        let mut m = Model::default();
        let body = pb::envelope::Body::Fault(pb::Fault {
            rows: vec![pb::fault::Row {
                level: "fault".into(),
                code: "OLED LOOP".into(),
                text: "the face loop stalled".into(),
                face: Some("OLED".into()),
            }],
            condition: Some("fault".into()),
            ts: None,
        });
        m.apply(
            "fault",
            &pb::Envelope {
                schema_version: 10,
                body: Some(body),
            },
        );
        assert_eq!(m.condition, "fault");
        assert_eq!(m.faults.len(), 1);
        assert_eq!(m.faults[0].1, "OLED LOOP");
    }

    #[test]
    fn the_event_line_sorts_its_detail() {
        let mut m = Model::default();
        let mut detail = std::collections::HashMap::new();
        detail.insert("verb".to_string(), "feel".to_string());
        detail.insert("feeling".to_string(), "busy".to_string());
        let body = pb::envelope::Body::Event(pb::Event {
            ts: 0.0,
            kind: "cmd".into(),
            detail,
        });
        m.apply("event", &loa_wire::envelope(body));
        assert_eq!(m.event, "cmd feeling=busy verb=feel");
    }
    #[test]
    fn weather_lands_the_folded_baro() {
        let mut m = Model::default();
        m.apply(
            "weather",
            &loa_wire::envelope(pb::envelope::Body::Weather(pb::Weather {
                temp_c: Some(21.5),
                hum_pct: Some(40.0),
                pressure_hpa: Some(1013.2),
                trend: Some("steady".into()),
                ..Default::default()
            })),
        );
        assert_eq!(m.pressure_hpa, Some(1013.2));
        assert_eq!(m.baro_trend, "steady");
    }
}
