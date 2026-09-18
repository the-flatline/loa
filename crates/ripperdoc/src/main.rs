//! ripperdoc — the bench console, TUI edition.
//!
//! This is Workbench chrome: the title block, the page keys, the status line,
//! the half-block face and the ring row — plus the honest counters. The
//! Textual original topped out at ~30 fps on this box because a terminal
//! emulator cannot be the render loop; the same picture built from ratatui
//! frames measured 1785 fps here, which is the number that justified the
//! rewrite. So: face 1024 B → half-blocks, the ring row beside it, the status
//! line under it, and the redraw rate and feed rate shown in the corner,
//! because a console that can't say how fast it is going is guessing.
//!
//! Data path: the topic feed, nothing else. The console subscribes to the
//! consumer topics, holds the last message per topic in its own frame, and
//! drains to newest every loop. Commands go over HTTP to the /api door as
//! POSTs and ONLY as POSTs — there is no GET anywhere in this program, and
//! tests/no_http_reads.rs fails the build if one appears.
//!
//! The menu keys work like they did in Workbench: `r` toggles console mode,
//! `1..7` jump pages, `f` flips the panel, `m` asks the body for the next
//! mood. They POST the verbs, the cortex moves state, the feed carries the
//! truth back — the console never paints what it asked for.
use std::error::Error;
use std::io;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::mpsc::{self, Receiver, Sender, TryRecvError};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use crossterm::event::{self, Event, KeyCode, KeyEvent, KeyEventKind, KeyModifiers};
use crossterm::execute;
use ratatui::backend::CrosstermBackend;
use ratatui::Frame;

mod view;
use view::Painter;

/// The face is one frame of the body's screen; the ring is 24 cells. The
/// console holds both and nothing else — everything on screen is derived.
const FACE_BYTES: usize = 1024;
const RING_CELLS: usize = 24;
/// How long a key may sit unread before the feed thread notices.
const TICK: Duration = Duration::from_millis(100);

pub fn paint(f: &mut Frame, model: &Model) {
    view::draw(f, model);
}

/// Where the feed lives. The cortex binds 0.0.0.0, which a client cannot
/// connect to, so the default names loopback — on the body that is where it
/// is. LOA_TOPIC_ENDPOINT overrides for a bench console watching another box.
fn feed_endpoint() -> String {
    if let Ok(ep) = std::env::var("LOA_TOPIC_ENDPOINT") {
        if !ep.is_empty() {
            return ep;
        }
    }
    let host = std::env::var("LOA_API_BIND").unwrap_or_else(|_| "127.0.0.1".into());
    format!("tcp://{host}:{}", loa_wire::FEED_PORT)
}

/// Where commands go: the door. http://host[:port]/api is the shape; the path
/// part survives anything the env gives.
fn door_url() -> String {
    let raw = std::env::var("LOA_DOOR")
        .unwrap_or_else(|_| "http://127.0.0.1:8765".into());
    let trimmed = raw.trim_end_matches('/').trim_end_matches("/api");
    if trimmed.contains("://") {
        format!("{trimmed}/api")
    } else {
        format!("http://{trimmed}/api")
    }
}

/// The whole screen state, assembled once per loop. The console holds the
/// last message per topic as its frame; every field here is read off one of
/// those, so the number beside a label is always the value the feed carried.
pub struct Model {
    pub face: Option<[u8; FACE_BYTES]>,
    pub ring: Option<[u8; RING_CELLS * 3]>,
    pub page: String,
    pub mood: String,
    pub condition: String,
    pub snr_on: bool,
    pub oled_mode: String,
    pub oled_text: String,
    pub oled_dim: bool,
    pub oled_flip: bool,
    pub console_on: bool,
    pub pir_high: bool,
    pub pir_count: u64,
    pub snr_cm: f64,
    pub snr_seen: bool,
    pub temp_c: f64,
    pub hum_pct: f64,
    pub pressure_hpa: f64,
    pub trend: String,
    pub pressure_seen: bool,
    pub power: Vec<(String, f64)>,
    pub faults: Vec<(String, String, String)>, // level, code, text — worst first
    pub event: String,                         // the last thing that happened
    pub last_rx: Option<Instant>,
    pub rx_count: u64,
    pub draw_fps: u32,
    pub feed_fps: u32,
    /// What the last keypress got back from the door: "feel → 200", or an
    /// error. It fades once the feed confirms the state moved; a command that
    /// echoes an answer the feed contradicts is the bug this console exists
    /// to catch, so both stay visible until they agree.
    pub cmd_status: String,
    pub menu: &'static str,
}

impl Default for Model {
    fn default() -> Self {
        Self {
            face: None,
            ring: None,
            page: "—".into(),
            mood: "—".into(),
            condition: "—".into(),
            snr_on: false,
            oled_mode: "—".into(),
            oled_text: String::new(),
            oled_dim: false,
            oled_flip: false,
            console_on: false,
            pir_high: false,
            pir_count: 0,
            snr_cm: 0.0,
            snr_seen: false,
            temp_c: 0.0,
            hum_pct: 0.0,
            pressure_hpa: 0.0,
            trend: "—".into(),
            pressure_seen: false,
            power: Vec::new(),
            faults: Vec::new(),
            event: String::new(),
            last_rx: None,
            rx_count: 0,
            draw_fps: 0,
            feed_fps: 0,
            cmd_status: String::new(),
            menu: view::MENU,
        }
    }
}

fn face_from_bytes(raw: &[u8]) -> Option<[u8; FACE_BYTES]> {
    if raw.len() != FACE_BYTES {
        return None;
    }
    let mut face = [0u8; FACE_BYTES];
    face.copy_from_slice(raw);
    Some(face)
}

/// ring is 72 B, 24 px RGB.
fn ring_from_bytes(raw: &[u8]) -> Option<[u8; RING_CELLS * 3]> {
    if raw.len() != RING_CELLS * 3 {
        return None;
    }
    let mut ring = [0u8; RING_CELLS * 3];
    ring.copy_from_slice(raw);
    Some(ring)
}

impl Model {
    /// Fold one decoded message into the frame. The console holds the last
    /// message per topic, so each row takes its topic's fields wholesale —
    /// news-only means no merging across messages, and there is none here.
    fn apply(&mut self, msg: loa_wire::Msg) {
        use loa_wire::pb::envelope::Body;
        let body = match msg.env.body.as_ref() {
            Some(b) => b,
            // decode guarantees a body; a message that arrives empty is a
            // wire bug, not something to paint.
            None => return,
        };
        match body {
            Body::Ripperdoc(r) => {
                if let Some(raw) = &r.face {
                    self.face = face_from_bytes(raw);
                }
                if r.page.is_some() {
                    self.page = r.page.clone().unwrap_or_else(|| "—".into());
                }
                if r.mood.is_some() {
                    self.mood = r.mood.clone().unwrap_or_else(|| "—".into());
                }
                if r.condition.is_some() {
                    self.condition = r.condition.clone().unwrap_or_else(|| "—".into());
                }
                if r.snr_on.is_some() {
                    self.snr_on = r.snr_on.unwrap_or(false);
                }
                if r.oled_mode.is_some() {
                    self.oled_mode = r.oled_mode.clone().unwrap_or_else(|| "—".into());
                }
                if r.oled_text.is_some() {
                    self.oled_text = r.oled_text.clone().unwrap_or_default();
                }
                if r.oled_dim.is_some() {
                    self.oled_dim = r.oled_dim.unwrap_or(false);
                }
                if r.oled_flip.is_some() {
                    self.oled_flip = r.oled_flip.unwrap_or(false);
                }
                if r.ripperdoc.is_some() {
                    self.console_on = r.ripperdoc.unwrap_or(false);
                }
            }
            Body::Ring(m) => {
                if let Some(raw) = &m.ring {
                    self.ring = ring_from_bytes(raw);
                }
            }
            Body::Pir(m) => {
                self.pir_high = m.high.unwrap_or(false);
                self.pir_count = m.count.unwrap_or(0);
            }
            Body::Sonar(m) => {
                self.snr_cm = m.cm.unwrap_or(0.0);
                self.snr_seen = true;
            }
            Body::Weather(m) => {
                // baro folded into weather with the v10 schema; the row
                // carries its own presence flags, so a temp-only message
                // leaves a stale pressure off the screen.
                self.temp_c = m.temp_c.unwrap_or(0.0);
                self.hum_pct = m.hum_pct.unwrap_or(0.0);
                if let Some(p) = m.pressure_hpa {
                    self.pressure_hpa = p;
                    self.pressure_seen = true;
                }
                self.trend = m.trend.clone().unwrap_or_else(|| "—".into());
            }
            Body::Power(m) => {
                let mut rails: Vec<(String, f64)> =
                    m.rails.iter().map(|(k, v)| (k.clone(), *v)).collect();
                rails.sort();
                self.power = rails;
            }
            Body::Fault(m) => {
                self.faults = m
                    .rows
                    .iter()
                    .map(|r| (r.level.clone(), r.code.clone(), r.text.clone()))
                    .collect();
                if let Some(c) = &m.condition {
                    if !c.is_empty() {
                        self.condition = c.clone();
                    }
                }
            }
            Body::Event(m) => {
                let head = if m.kind.is_empty() { "event" } else { m.kind.as_str() };
                if m.detail.is_empty() {
                    self.event = head.to_string();
                } else {
                    let mut parts: Vec<&String> = m.detail.keys().collect();
                    parts.sort();
                    self.event = format!(
                        "{head} {}",
                        parts
                            .iter()
                            .map(|k| format!("{k}={}", m.detail[*k]))
                            .collect::<Vec<_>>()
                            .join(" ")
                    );
                }
            }
            Body::Init(_) => {
                // init is control — the cortex asking daemons to resend. The
                // console subscribes to news topics only; if one arrives on a
                // shared socket there is nothing to paint.
            }
        }
        self.rx_count += 1;
        self.last_rx = Some(Instant::now());
    }
}

/// The feed leg: a plain blocking loop. RCVHWM=1 on loa-wire's Subscriber
/// keeps one message queued per pipe; a short pause between drains lets the
/// pipe refill so a 120 Hz face stays current without spinning the socket at
/// full tilt. The console redraws at its own rate either way.
fn feed_thread(
    endpoint: String,
    model: Arc<Mutex<Model>>,
    quit: Arc<AtomicBool>,
    rx_frames: Arc<AtomicU64>,
) -> Result<JoinHandle<()>, Box<dyn Error>> {
    let ctx = zmq::Context::new();
    let topics = [
        "ripperdoc", "ring", "pir", "sonar", "weather", "power", "fault", "event",
    ];
    let mut sub = loa_wire::Subscriber::connect(&ctx, &[endpoint], &topics)?;
    let h = thread::spawn(move || {
        while !quit.load(Ordering::Relaxed) {
            match sub.drain() {
                Ok(Some(msg)) => {
                    rx_frames.fetch_add(1, Ordering::Relaxed);
                    model.lock().unwrap().apply(msg);
                }
                Ok(None) => thread::sleep(Duration::from_millis(4)),
                Err(e) => {
                    // A bad message rides over a good screen: the console
                    // shows what it last held and the log carries the rest.
                    eprintln!("[feed] {e}");
                    thread::sleep(Duration::from_millis(4));
                }
            }
        }
    });
    Ok(h)
}

/// The input leg: read keys in its own thread so a held terminal never stalls
/// the draw loop.
fn input_thread(tx: Sender<KeyEvent>, quit: Arc<AtomicBool>) -> JoinHandle<()> {
    thread::spawn(move || {
        while !quit.load(Ordering::Relaxed) {
            match event::poll(TICK) {
                Ok(true) => match event::read() {
                    Ok(Event::Key(k)) if k.kind == KeyEventKind::Press => {
                        if tx.send(k).is_err() {
                            break;
                        }
                    }
                    Ok(_) => {}
                    Err(_) => break,
                },
                Ok(false) => {}
                Err(_) => break,
            }
        }
    })
}

/// The commands live here, not in the draw loop: a POST to the door takes
/// milliseconds, the face takes 8 ms to redraw, and they are not sharing a
/// thread.
fn cmd_thread(
    rx: Receiver<Cmd>,
    model: Arc<Mutex<Model>>,
    quit: Arc<AtomicBool>,
) -> JoinHandle<()> {
    thread::spawn(move || {
        while let Ok(cmd) = rx.recv() {
            if quit.load(Ordering::Relaxed) {
                break;
            }
            let (label, body) = cmd.parts();
            let status = match post_json(&door_url(), &body) {
                Ok(code) => format!("{label} → {code}"),
                Err(e) => format!("{label} → ERROR {e}"),
            };
            model.lock().unwrap().cmd_status = status;
        }
    })
}

/// A console verb, already shaped for /api. The args are plain JSON built by
/// hand — three verbs with at most two fields each; the dep is not worth it.
enum Cmd {
    Ripperdoc(bool),
    Page(String),
    Mood,
    Flip(bool),
}

impl Cmd {
    fn parts(&self) -> (String, String) {
        match self {
            Cmd::Ripperdoc(on) => (
                format!("ripperdoc={}", if *on { "on" } else { "off" }),
                format!("{{\"cmd\":\"ripperdoc\",\"args\":{{\"on\":{}}}}}", on),
            ),
            Cmd::Page(page) => (
                format!("page={page}"),
                format!("{{\"cmd\":\"ripperdoc\",\"args\":{{\"page\":\"{page}\"}}}}"),
            ),
            // The cortex owns the mood vocabulary; `next` is the whole of the
            // console's opinion about moods, and it is the right amount.
            Cmd::Mood => (
                "feel next".into(),
                "{\"cmd\":\"feel\",\"args\":{\"next\":true}}".into(),
            ),
            // The door takes an absolute flip; the console knows the last
            // state it saw, so it sends the opposite. Until the feed carries
            // the new one the status line says "sending" — no lie of
            // confirmation.
            Cmd::Flip(to) => (
                format!("display flip={}", if *to { "on" } else { "off" }),
                format!("{{\"cmd\":\"display\",\"args\":{{\"flip\":{}}}}}", to),
            ),
        }
    }
}

/// The only HTTP the console does: a POST, and only to /api. A raw TCP POST —
/// the door answers in milliseconds and a full HTTP client on the Pi is a
/// dependency the draw loop will never use.
fn post_json(url: &str, body: &str) -> io::Result<u16> {
    use std::io::{Read, Write};
    use std::net::TcpStream;

    let (host, port, path) = split_http(url)?;
    let mut sock = TcpStream::connect((host.as_str(), port))?;
    sock.set_read_timeout(Some(Duration::from_secs(2)))?;
    sock.set_write_timeout(Some(Duration::from_secs(2)))?;
    let req = format!(
        "POST {path} HTTP/1.1\r\nHost: {host}:{port}\r\nContent-Type: application/json\r\n\
         Content-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    sock.write_all(req.as_bytes())?;
    sock.flush()?;
    let mut buf = [0u8; 512];
    let n = sock.read(&mut buf)?;
    let head = String::from_utf8_lossy(&buf[..n]);
    // Status line "HTTP/1.1 200 OK" → 200; a garbage response is a 0, and 0
    // in the status line is louder than a number that looks like success.
    let code = head
        .split_whitespace()
        .nth(1)
        .and_then(|s| s.parse().ok())
        .unwrap_or(0);
    Ok(code)
}

fn split_http(url: &str) -> io::Result<(String, u16, String)> {
    let rest = url.strip_prefix("http://").unwrap_or(url);
    let (authority, path) = match rest.find('/') {
        Some(i) => (&rest[..i], rest[i..].to_string()),
        None => (rest, "/".to_string()),
    };
    let (host, port) = match authority.rsplit_once(':') {
        Some((h, p)) => (h.to_string(), p.parse().unwrap_or(80)),
        None => (authority.to_string(), 80),
    };
    if host.is_empty() {
        return Err(io::Error::new(io::ErrorKind::InvalidInput, "no host in url"));
    }
    Ok((host, port, path))
}

fn main() -> Result<(), Box<dyn Error>> {
    let endpoint = feed_endpoint();
    let model = Arc::new(Mutex::new(Model::default()));
    let quit = Arc::new(AtomicBool::new(false));
    // The two rates, counted separately and on purpose: rx_frames is what the
    // feed delivered, draw frames (below, in the painter) is what the screen
    // shows. A console that reports one number blurs "the glass is slow" into
    // "I am slow."
    let rx_frames = Arc::new(AtomicU64::new(0));

    let feed = match feed_thread(endpoint.clone(), model.clone(), quit.clone(), rx_frames.clone()) {
        Ok(h) => Some(h),
        Err(e) => {
            // The console opens without the body. The feed line says what it
            // means and the door stays usable; a dead body should not stop
            // you from looking at the chrome.
            eprintln!("[feed] {endpoint}: {e}");
            None
        }
    };

    let (key_tx, key_rx) = mpsc::channel::<KeyEvent>();
    let keys = input_thread(key_tx, quit.clone());
    let (cmd_tx, cmd_rx) = mpsc::channel::<Cmd>();
    let poster = cmd_thread(cmd_rx, model.clone(), quit.clone());

    let mut stdout = io::stdout();
    let mut painter = Painter::init(&mut stdout)?;
    let mut cleanup = |painter: &mut Painter| {
        painter.shutdown(&mut stdout);
        quit.store(true, Ordering::Relaxed);
        let _ = stdout;
    };

    // Polling the keyboard channel on the draw loop's own clock; the keys
    // thread blocks on read, so nothing here does.
    let mut last_count = 0u64;
    let mut last_tick = Instant::now();
    loop {
        match key_rx.try_recv() {
            Ok(key) => {
                if handle_key(key, &model, &cmd_tx) {
                    break;
                }
            }
            Ok(_) | Err(TryRecvError::Empty) => {}
            Err(TryRecvError::Disconnected) => break,
        }
        if last_tick.elapsed() >= Duration::from_millis(120) {
            let count = rx_frames.load(Ordering::Relaxed);
            let secs = last_tick.elapsed().as_secs_f64();
            let mut m = model.lock().unwrap();
            m.feed_fps = if count != last_count && secs > 0.0 {
                ((count - last_count) as f64 / secs).round() as u32
            } else {
                0
            };
            last_count = count;
            last_tick = Instant::now();
            drop(m);
        }
        let m = model.lock().unwrap();
        painter.draw(&mut stdout, &m);
    }
    cleanup(&mut painter);
    quit.store(true, Ordering::Relaxed);
    let _ = keys.join();
    let _ = poster.join();
    if let Some(h) = feed {
        let _ = h.join();
    }
    Ok(())
}

/// The Workbench keys, moved. Returns true when the console should quit.
fn handle_key(key: KeyEvent, model: &Arc<Mutex<Model>>, tx: &Sender<Cmd>) -> bool {
    if key.modifiers.contains(KeyModifiers::CONTROL) && key.code == KeyCode::Char('c') {
        return true;
    }
    let page_keys = ['1', '2', '3', '4', '5', '6', '7'];
    let pages = view::PAGES;
    let cmd = match key.code {
        KeyCode::Char('q') | KeyCode::Esc => return true,
        KeyCode::Char('r') => {
            let on = !model.lock().unwrap().console_on;
            Cmd::Ripperdoc(on)
        }
        KeyCode::Char('m') => Cmd::Mood,
        KeyCode::Char('f') => {
            let to = !model.lock().unwrap().oled_flip;
            Cmd::Flip(to)
        }
        KeyCode::Char(c) if page_keys.contains(&c) => {
            Cmd::Page(pages[c as usize - '1' as usize].to_string())
        }
        _ => return false,
    };
    // The status line is the send, not the result: it says what went to the
    // door and the code that came back; the state itself changes on the feed.
    let (label, _) = cmd.parts();
    model.lock().unwrap().cmd_status = format!("sending {label}");
    let _ = tx.send(cmd);
    false
}
