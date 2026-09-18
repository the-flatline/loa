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

use std::io::{self, Read, Write};
use std::net::TcpStream;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use crossterm::event::{self, Event, KeyCode};
use ratatui::Frame;

use loa_wire::{Subscriber, FEED_PORT};
use view::{Model, PAGES};

/// The consumer topics. `state` is the device leg and is not for this pane;
/// baro folded into weather; `init` is control for late joiners and the
/// console joins watching live news, not a snapshot.
const TOPICS: [&str; 8] = [
    "ripperdoc",
    "ring",
    "pir",
    "sonar",
    "weather",
    "power",
    "fault",
    "event",
];

fn main() {
    // The feed reads in one task; the draw loop owns the terminal. Nothing
    // blocks the other.
    let shared = Arc::new(Mutex::new(Model::default()));
    let feed_state = Arc::clone(&shared);
    // A plain thread with a BLOCKING drain (1ms poll), not an async runtime:
    // the feed is one socket and the draw loop owns the terminal, so there is
    // nothing here for a runtime to schedule. Subscriber::drain keeps only the
    // newest of what arrived, so the reader can never fall behind into a
    // backlog it has to work through.
    std::thread::spawn(move || {
        let endpoints = feed_endpoints();
        let ctx = zmq::Context::new();
        let mut sub = match Subscriber::connect(&ctx, &endpoints, &TOPICS) {
            Ok(s) => s,
            Err(e) => {
                eprintln!("ripperdoc: no feed at {endpoints:?}: {e}");
                return;
            }
        };
        loop {
            match sub.drain() {
                Ok(Some(msg)) => {
                    feed_state.lock().unwrap().apply(&msg.topic, &msg.env);
                }
                Ok(None) => std::thread::sleep(Duration::from_millis(1)),
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

/// Where to find the feed. LOA_TOPIC_ENDPOINT is the whole answer when set;
/// otherwise the body's own bind host, loopback by default. The cortex binds
/// 0.0.0.0, which a client cannot dial — a remote console names the body.
fn feed_endpoints() -> Vec<String> {
    if let Ok(ep) = std::env::var("LOA_TOPIC_ENDPOINT") {
        if !ep.is_empty() {
            return vec![ep];
        }
    }
    let host = std::env::var("LOA_API_BIND").unwrap_or_else(|_| "127.0.0.1".into());
    vec![format!("tcp://{host}:{FEED_PORT}")]
}

/// Where the door answers. One env, one URL, one verb path.
fn door_url() -> String {
    std::env::var("LOA_DOOR").unwrap_or_else(|_| "http://127.0.0.1:8765/api".into())
}

fn run(terminal: &mut ratatui::DefaultTerminal, shared: &Arc<Mutex<Model>>) -> io::Result<()> {
    // Two windows, both reset per second: what THIS program drew, and how many
    // face frames the feed actually delivered. They are different numbers and
    // the corner shows both — a console reporting only its own redraw rate
    // hides a dead feed behind a busy screen.
    let mut drawn = 0u64;
    let mut since = Instant::now();
    let mut fps = 0.0f64;
    let mut feed_frames = 0u64;
    let mut last_total = 0u64;
    let mut feed_fps = 0.0f64;
    let mut quiet_since: Option<Instant> = None;
    let mut last_seen = 0u64;

    loop {
        let v = shared.lock().unwrap().clone();
        if v.frames != last_seen {
            last_seen = v.frames;
            quiet_since = Some(Instant::now());
        }
        let age = quiet_since
            .map(|t| t.elapsed().as_secs_f64())
            .unwrap_or(99.0);

        feed_frames += v.frames - last_total;
        last_total = v.frames;

        terminal.draw(|f| draw(f, &v, age, fps, feed_fps))?;
        drawn += 1;
        if since.elapsed() >= Duration::from_secs(1) {
            fps = drawn as f64 / since.elapsed().as_secs_f64();
            feed_fps = feed_frames as f64 / since.elapsed().as_secs_f64();
            drawn = 0;
            feed_frames = 0;
            since = Instant::now();
        }
        // Keys with a zero poll: the draw rate is not the key rate, and a
        // console that waits on input cannot redraw. The loop's own cost is
        // the throttle.
        if event::poll(Duration::ZERO)? {
            if let Event::Key(k) = event::read()? {
                if let Some(cmd) = command(&v, k.code) {
                    send(cmd);
                } else if matches!(k.code, KeyCode::Char('q') | KeyCode::Esc) {
                    return Ok(());
                }
            }
        }
    }
}

/// The key map, returning the body of a POST. None is "a key we do not use".
fn command(v: &Model, code: KeyCode) -> Option<String> {
    let body = match code {
        // The console flag is on the wire — the body says whether it is in
        // console mode, so the key toggles what the BODY reports, not a guess
        // about it.
        KeyCode::Char('r') => {
            format!(
                "{{\"cmd\":\"ripperdoc\",\"args\":{{\"on\":{}}}}}",
                !v.ripperdoc
            )
        }
        KeyCode::Char(c @ '1'..='7') => format!(
            "{{\"cmd\":\"ripperdoc\",\"args\":{{\"page\":\"{}\"}}}}",
            PAGES[c as usize - '1' as usize]
        ),
        // The cortex owns the mood vocabulary; the console only asks for the
        // next one. That comment was earned once and stays earned.
        KeyCode::Char('m') => "{\"cmd\":\"feel\",\"args\":{\"next\":true}}".into(),
        KeyCode::Char('f') => {
            format!(
                "{{\"cmd\":\"display\",\"args\":{{\"flip\":{}}}}}",
                !v.oled_flip
            )
        }
        _ => return None,
    };
    Some(body)
}

/// Fire the POST off-thread: a keypress is one-shot, the door takes
/// milliseconds, and the face takes half one — they do not share a thread.
fn send(body: String) {
    let url = door_url();
    std::thread::spawn(move || {
        if let Err(e) = post_json(&url, &body) {
            eprintln!("ripperdoc: command failed: {e}");
        }
    });
}

/// The whole HTTP client: thirty lines, no crate. A door that answers with
/// anything else is the door's problem; this reads the status line and leaves.
fn post_json(url: &str, body: &str) -> io::Result<u16> {
    let rest = url
        .strip_prefix("http://")
        .ok_or_else(|| io::Error::other("door url must be http://host:port/path"))?;
    let (authority, path) = match rest.split_once('/') {
        Some((a, p)) => (a, format!("/{p}")),
        None => (rest, "/".to_string()),
    };
    let (host, port) = authority
        .rsplit_once(':')
        .ok_or_else(|| io::Error::other("door url must name a port"))?;
    let port: u16 = port
        .parse()
        .map_err(|e| io::Error::other(format!("door port: {e}")))?;

    let mut sock = TcpStream::connect((host, port))?;
    sock.set_write_timeout(Some(Duration::from_secs(5)))?;
    sock.set_read_timeout(Some(Duration::from_secs(5)))?;
    write!(
        sock,
        "POST {path} HTTP/1.1\r\nHost: {authority}\r\nContent-Type: application/json\r\n\
         Content-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    )?;
    sock.flush()?;
    let mut resp = [0u8; 128];
    let n = sock.read(&mut resp)?;
    let head = String::from_utf8_lossy(&resp[..n]);
    let code: u16 = head
        .split_whitespace()
        .nth(2)
        .and_then(|s| s.parse().ok())
        .unwrap_or(0);
    if code >= 400 {
        return Err(io::Error::other(format!("door answered {code}")));
    }
    Ok(code)
}

fn draw(f: &mut Frame, v: &Model, age: f64, fps: f64, feed_fps: f64) {
    view::draw(f, v, age, fps, feed_fps);
}
