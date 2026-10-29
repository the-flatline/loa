//! ripperdoc — the bench console, in Rust. Workbench 1.3 chrome, a live face
//! and ring drawn from the feed, and a redraw rate it reports honestly.
//!
//! Same contract as the Python console, and the same two rules:
//!
//!   * EVERYTHING DRAWN COMES OFF THE TOPIC. There is no HTTP read path. The
//!     console holds the last frame and draws it; a silent feed says so.
//!   * COMMANDS GO OVER ONE DOOR. POST /api with one of the three verbs. No
//!     route was added for this console and none is needed.
//!
//! Why it exists: Textual spent 21.6ms redrawing a FIVE CHARACTER frame — a
//! widget tree, a layout pass and a compositor, per update, content-independent
//! — which put a 30fps ceiling on a pane that needs 120. Measured 2026-09-13.
//! Ratatui builds a cell buffer and diffs it: 1785 fps for this same 128x32
//! half-block face, 0.47ms/frame, so the console is no longer the wall.

mod view;

use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use crossterm::event::{self, Event, KeyCode};
use ratatui::layout::{Constraint, Direction, Layout};
use ratatui::style::{Color, Style};
use ratatui::text::{Line, Span, Text};
use ratatui::widgets::{Block, Borders, Paragraph};
use ratatui::Frame;

use loa_topic::{feed_endpoints, Subscriber};
use view::View;

const BLUE: Color = Color::Rgb(0x0F, 0x0F, 0xAF);
const WHITE: Color = Color::Rgb(0xFF, 0xFF, 0xFF);
const ORANGE: Color = Color::Rgb(0xFF, 0x99, 0x00);
const BACKDROP: Color = Color::Rgb(0x0A, 0x0E, 0x12);
const DIM: Color = Color::Rgb(0x23, 0x2C, 0x38);

const MENUS: &str = "  r ripperdoc   1 sensors   2 pir   3 snr   4 temp   5 frag   6 power   7 fault   m mood   f flip   q quit";

fn main() {
    // The feed reads in one task; the draw loop owns the terminal. Nothing
    // blocks the other.
    let shared = Arc::new(Mutex::new(View::default()));
    let feed_state = Arc::clone(&shared);
    // A plain thread with a BLOCKING recv (1ms poll), not an async runtime:
    // the feed is one socket and the draw loop owns the terminal, so there is
    // nothing here for a runtime to schedule. CONFLATE is on the socket, so the
    // reader can never fall behind into a backlog.
    std::thread::spawn(move || {
        let topics = ["ripperdoc", "ring", "pir", "sonar", "baro", "weather",
                      "power", "fault"];
        let endpoints = feed_endpoints();
        let mut sub = match Subscriber::connect(&endpoints, &topics) {
            Ok(s) => s,
            Err(e) => {
                eprintln!("ripperdoc: no feed at {endpoints:?}: {e}");
                return;
            }
        };
        loop {
            match sub.drain() {
                Ok(batch) => {
                    if batch.is_empty() {
                        std::thread::sleep(Duration::from_millis(1));
                        continue;
                    }
                    let mut v = feed_state.lock().unwrap();
                    for msg in &batch {
                        v.apply(&msg.topic, &msg.env);
                    }
                }
                Err(e) => {
                    eprintln!("ripperdoc: feed error: {e}");
                    std::thread::sleep(Duration::from_millis(100));
                }
            }
        }
    });

    let mut terminal = ratatui::init();
    let res = run(&mut terminal, &shared);
    ratatui::restore();
    if let Err(e) = res {
        eprintln!("ripperdoc: {e}");
        std::process::exit(1);
    }
}

fn run(
    terminal: &mut ratatui::DefaultTerminal,
    shared: &Arc<Mutex<View>>,
) -> std::io::Result<()> {
    let mut drawn = 0u64;
    let mut since = Instant::now();
    let mut reported = Instant::now();
    let mut quiet_since: Option<Instant> = None;
    let mut last_frames = 0u64;
    loop {
        let v = shared.lock().unwrap().clone();
        if v.frames != last_frames {
            last_frames = v.frames;
            quiet_since = Some(Instant::now());
        }
        let age = quiet_since.map(|t| t.elapsed().as_secs_f64()).unwrap_or(99.0);
        terminal.draw(|f| draw(f, &v, age, drawn as f64 / since.elapsed().as_secs_f64()))?;
        drawn += 1;
        if reported.elapsed() >= Duration::from_secs(2) {
            reported = Instant::now();
        }
        if since.elapsed() >= Duration::from_secs(10) {
            since = Instant::now();
            drawn = 0;
        }
        // Keys with a short poll: the draw rate is not the key rate, and a
        // console that waits on input cannot redraw.
        if event::poll(Duration::from_millis(8))? {
            if let Event::Key(k) = event::read()? {
                match k.code {
                    KeyCode::Char('q') | KeyCode::Esc => return Ok(()),
                    KeyCode::Char('r') => post("ripperdoc", format!("{{\"on\":{}}}", !v_following(&v))),
                    KeyCode::Char('1') => post("ripperdoc", "{\"page\":\"sensors\"}".into()),
                    KeyCode::Char('2') => post("ripperdoc", "{\"page\":\"pir\"}".into()),
                    KeyCode::Char('3') => post("ripperdoc", "{\"page\":\"snr\"}".into()),
                    KeyCode::Char('4') => post("ripperdoc", "{\"page\":\"temp\"}".into()),
                    KeyCode::Char('5') => post("ripperdoc", "{\"page\":\"frag\"}".into()),
                    KeyCode::Char('6') => post("ripperdoc", "{\"page\":\"power\"}".into()),
                    KeyCode::Char('7') => post("ripperdoc", "{\"page\":\"fault\"}".into()),
                    KeyCode::Char('f') => post("display", format!("{{\"flip\":{}}}", !v.oled_flip)),
                    KeyCode::Char('m') => post("feel", "{\"next\":true}".into()),
                    _ => {}
                }
            }
        }
    }
}

/// The console flag is on the wire — the body says whether it is in console
/// mode, so the key toggles what the BODY reports, not a guess about it.
fn v_following(v: &View) -> bool {
    v.oled_mode == "ripperdoc"
}

fn post(cmd: &str, args: String) {
    let host = std::env::var("LOA_API_BIND").unwrap_or_else(|_| "127.0.0.1".into());
    let url = format!("http://{host}:8765/api");
    let body = format!("{{\"cmd\":\"{cmd}\",\"args\":{args}}}");
    std::thread::spawn(move || {
        let r = ureq::post(&url)
            .set("Content-Type", "application/json")
            .timeout(Duration::from_secs(5))
            .send_string(&body);
        if let Err(e) = r {
            eprintln!("ripperdoc: command failed: {e}");
        }
    });
}

fn draw(f: &mut Frame, v: &View, age_s: f64, fps: f64) {
    let area = f.area();
    let rows = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(1),   // menubar
            Constraint::Length(3),   // status
            Constraint::Min(32),     // live: face + ring
            Constraint::Min(0),      // log / footer
        ])
        .split(area);

    f.render_widget(
        Paragraph::new(MENUS).style(Style::default().bg(BLUE).fg(WHITE)),
        rows[0],
    );

    // The status line: the body's own words, and how old the picture is.
    let mut status = v.status(age_s);
    status.push_str(&format!("   {fps:.0} fps"));
    f.render_widget(
        Paragraph::new(status)
            .style(Style::default().bg(Color::Black).fg(WHITE))
            .block(Block::default().borders(Borders::ALL).border_style(Style::default().fg(BLUE))),
        rows[1],
    );

    let live = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([Constraint::Length(130), Constraint::Min(28)])
        .split(rows[2]);

    // FACE — the panel's own bytes, one cell per 1x2 pixels, no scaling.
    let art: Text = if v.face.len() == 1024 && age_s < 5.0 {
        view::face_art(&v.face)
            .into_iter()
            .map(Line::from)
            .collect::<Vec<_>>()
            .into()
    } else {
        Text::from(if age_s >= 5.0 {
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
        live[0],
    );

    // RING — 24 px of RGB, straight off the wire, drawn as the ring itself.
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
    let ring_text = if v.ring.len() >= 72 {
        Text::from(vec![
            Line::from(leds),
            Line::from(""),
            Line::from(format!("state  {}", v.ring_state)),
            Line::from(format!("mode   {}", v.oled_mode)),
        ])
    } else {
        Text::from("RING OFFLINE")
    };
    f.render_widget(
        Paragraph::new(ring_text)
            .style(Style::default().fg(WHITE))
            .block(Block::default().title("RING").borders(Borders::ALL)
                .border_style(Style::default().fg(BLUE))),
        live[1],
    );

    // The log pane is the console's own line about the body's condition.
    let cond = if v.condition.is_empty() { "?" } else { &v.condition };
    let mut lines = vec![Line::from(format!(
        "condition {cond}   faults {}   frames {}   flip {}   dim {}",
        v.faults.len(),
        v.frames,
        v.oled_flip,
        v.oled_dim
    ))];
    for (level, code, text) in v.faults.iter().take(6) {
        lines.push(Line::from(Span::styled(
            format!("  {level:<5} {code:<12} {text}"),
            Style::default().fg(if level == "fault" { ORANGE } else { WHITE }),
        )));
    }
    if let (Some(t), Some(h)) = (v.temp_c, v.hum_pct) {
        lines.push(Line::from(format!("  weather {t:.1}C {h:.0}%")));
    }
    if let Some(p) = v.pressure_hpa {
        lines.push(Line::from(format!("  baro    {p:.1} hPa {}", v.baro_trend)));
    }
    f.render_widget(
        Paragraph::new(Text::from(lines))
            .style(Style::default().bg(BACKDROP).fg(WHITE))
            .block(Block::default().title("LOG").borders(Borders::ALL)
                .border_style(Style::default().fg(DIM))),
        rows[3],
    );
}
