//! The ring daemon. State down, frames up.
//!
//! Subscribes to the three topics the tell is made of — `state`, `fault`,
//! `event` — holds the last of each, and renders on its OWN clock: there is
//! no tick in this system, the animation clock is internal, and every frame
//! it produces is news. Each finished 72-byte frame goes two places: to the
//! strip over SPI1, and back UP to the cortex on the inbound PUSH as topic
//! `ring` (the cortex relays ring->ring for consumers). The daemon never
//! subscribes to consumer `ring` — that would be an echo of its own output.
//!
//! The tell precedence, verbatim from ring.py: alarm > hurts > mute > busy >
//! one-shot > home. The mute gate is the DEVICE's: never received a fault
//! message reads as mute, whatever the condition word says (ring.py:31-40).
//!
//! The SPI write is ~2ms of blocking syscalls per frame, so ONE thread owns
//! the strip and reads frames off a channel; the async half never touches
//! the fd. The render is uncapped (the strip's write dominates on the body);
//! the writer drains to newest, so a queue is never a backlog of the past.
//!
//! No strip (this bench box): opening /dev/spidev1.0 fails, and that is a
//! clear error and an exit — unless --no-strip says so on purpose, in which
//! case the render path still runs and the frames still go up.

use std::process;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc;
use std::sync::Arc;
use std::time::{Duration, Instant};

use loa_wire::{aio, decode, envelope, pb, Sender, FEED_PORT, INBOUND_PORT};

use loa_ring::encode::{Ring, DEFAULT_HZ};
use loa_ring::render::{wall_now, Renderer};
use loa_ring::tells::{device_condition, RingInput};

/// The topics the ring draws FROM. Never `ring`: that is the ring's own
/// frame relayed back by the cortex.
const TOPICS: [&str; 3] = ["state", "fault", "event"];

/// The yield between renders. Not a cap: on the body the SPI syscall sets
/// the pace, and here it just keeps the reactor a guest and not a squatter.
const PARK: Duration = Duration::from_millis(1);

struct Args {
    feed: String,
    inbound: String,
    spi: String,
    hz: u32,
    secs: Option<u64>,
    no_strip: bool,
}

fn usage() -> ! {
    eprintln!(
        "usage: loa-ring [--feed URL] [--inbound URL] [--spi PATH] [--hz N] [--secs N] [--no-strip]"
    );
    process::exit(2);
}

fn parse_args() -> Args {
    let mut a = Args {
        // The cortex's PUB: LOA_TOPIC_ENDPOINT names the host, the feed port
        // is the wire's contract.
        feed: format!(
            "tcp://{}:{}",
            std::env::var("LOA_TOPIC_ENDPOINT").unwrap_or_else(|_| "127.0.0.1".into()),
            FEED_PORT
        ),
        inbound: std::env::var("LOA_INBOUND")
            .unwrap_or_else(|_| format!("tcp://127.0.0.1:{INBOUND_PORT}")),
        spi: std::env::var("LOA_RING_SPI").unwrap_or_else(|_| "/dev/spidev1.0".into()),
        hz: std::env::var("LOA_RING_HZ")
            .ok()
            .and_then(|s| s.parse().ok())
            .unwrap_or(DEFAULT_HZ),
        secs: None,
        no_strip: false,
    };
    let argv: Vec<String> = std::env::args().collect();
    fn take(argv: &[String], i: &mut usize, name: &str) -> String {
        *i += 1;
        argv.get(*i).cloned().unwrap_or_else(|| {
            eprintln!("loa-ring: {name} wants a value");
            process::exit(2);
        })
    }
    let mut i = 1;
    while i < argv.len() {
        match argv[i].as_str() {
            "--feed" => a.feed = take(&argv, &mut i, "--feed"),
            "--inbound" => a.inbound = take(&argv, &mut i, "--inbound"),
            "--spi" => a.spi = take(&argv, &mut i, "--spi"),
            "--hz" => {
                a.hz = take(&argv, &mut i, "--hz")
                    .parse()
                    .unwrap_or_else(|_| usage())
            }
            "--secs" => {
                a.secs = Some(
                    take(&argv, &mut i, "--secs")
                        .parse()
                        .unwrap_or_else(|_| usage()),
                )
            }
            "--no-strip" => a.no_strip = true,
            "--help" | "-h" => usage(),
            other => {
                eprintln!("loa-ring: not an argument I know: {other}");
                usage();
            }
        }
        i += 1;
    }
    a
}

/// The last message of each subscribed topic, as one snapshot the renderer
/// reads. Holding is the whole memory the tell needs on this side; the
/// renderer holds its own clock state.
#[derive(Default)]
struct Held {
    /// state.ring_state; empty means absent, which renders as "home"
    /// (ring.py:111 `or "home"`).
    ring_state: String,
    /// state.ripperdoc — the console flag, switches the scan cadence
    /// (ring.py:177).
    ripperdoc: bool,
    /// The condition words, both legs kept because they disagree on purpose
    /// (loa-cortex build.rs: the FAULT recomputes from rows, the state leg
    /// carries the raw word). The fault's own word wins.
    state_cond: Option<String>,
    fault_cond: Option<String>,
    /// The device's mute gate: has THIS process ever heard a fault message.
    fault_seen: bool,
}

impl Held {
    fn input(&self) -> RingInput {
        let cond = device_condition(
            self.fault_seen,
            self.fault_cond.as_deref().or(self.state_cond.as_deref()),
        );
        let ring_state = if self.ring_state.is_empty() {
            "home".into()
        } else {
            self.ring_state.clone()
        };
        RingInput {
            cond,
            ring_state,
            ripperdoc: self.ripperdoc,
        }
    }
}

/// Take one decoded message into the held view and the one-shot memory
/// (ring.py:101-106: an event kind "ring" with detail.state scan|glitch is
/// a one-shot RECORD — the renderer dedupes by (kind, ts) and gates play
/// on ring_state == "home").
fn hold(held: &mut Held, renderer: &mut Renderer, msg: &loa_wire::Msg) {
    let Some(body) = &msg.env.body else { return };
    match body {
        pb::envelope::Body::Ripperdoc(m) if msg.topic == "state" => {
            held.ring_state = m.ring_state.clone().unwrap_or_default();
            held.ripperdoc = m.ripperdoc.unwrap_or(false);
            held.state_cond = m.condition.clone();
        }
        pb::envelope::Body::Fault(m) => {
            held.fault_seen = true;
            held.fault_cond = m.condition.clone();
        }
        pb::envelope::Body::Event(e) => {
            if e.kind == "ring" {
                if let Some(state) = e.detail.get("state") {
                    renderer.note_oneshot(state, e.ts);
                }
            }
        }
        _ => {} // init and anything else the feed carries is not the ring's
    }
}

/// The strip thread. Blocking by design — SPI is syscalls, and a reactor
/// must not park on them. Drain to newest: a frame nobody has drawn yet
/// supersedes every frame before it; the newest is the only one worth
/// sending.
fn strip_worker(mut ring: Ring, rx: mpsc::Receiver<[u8; 72]>, written: Arc<AtomicU64>) {
    // A ring must never sit on the last process's frozen colour.
    if let Err(e) = ring.off() {
        eprintln!("loa-ring: off: {e}");
    }
    loop {
        let Ok(first) = rx.recv() else { return }; // sender gone: exit
        let mut newest = first;
        while let Ok(f) = rx.try_recv() {
            newest = f;
        }
        if let Err(e) = ring.show(&newest) {
            eprintln!("loa-ring: show: {e}");
            continue;
        }
        written.fetch_add(1, Ordering::Relaxed);
    }
}

/// The rate line: what this process produced and what the strip took, every
/// 5s on stderr. The animation clock is internal, so these counters are the
/// only instrument the ring has.
fn report(rendered: &AtomicU64, written: &AtomicU64, at: &mut Instant) {
    let el = at.elapsed().as_secs_f64();
    if el >= 5.0 {
        eprintln!(
            "loa-ring: {:.0} rendered/s, {:.0} wrote/s",
            rendered.swap(0, Ordering::Relaxed) as f64 / el,
            written.swap(0, Ordering::Relaxed) as f64 / el,
        );
        *at = Instant::now();
    }
}

#[tokio::main]
async fn main() {
    let args = parse_args();

    // --- the strip: one thread, one owner, or an explicit bench pass -----
    let written = Arc::new(AtomicU64::new(0));
    let tx: Option<mpsc::Sender<[u8; 72]>> = if args.no_strip {
        eprintln!("loa-ring: --no-strip: frames render and ride up, nothing hits the bus");
        None
    } else {
        match Ring::open(&args.spi, args.hz) {
            Ok(ring) => {
                let (s, rx) = mpsc::channel();
                let counter = Arc::clone(&written);
                std::thread::spawn(move || strip_worker(ring, rx, counter));
                Some(s)
            }
            Err(e) => {
                // On the body this is fatal and says so. Off-Pi it is the
                // expected story: pass --no-strip and the daemon still runs.
                eprintln!("loa-ring: no ring on {}: {e}", args.spi);
                eprintln!("loa-ring: pass --no-strip to run without the strip");
                process::exit(1);
            }
        }
    };

    // --- the wire ----------------------------------------------------------
    let ctx = zmq::Context::new();
    let sub = match aio::AInbound::sub(&ctx, &[args.feed.clone()], &TOPICS) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("loa-ring: cannot subscribe to {}: {e}", args.feed);
            process::exit(1);
        }
    };
    // The out-leg to the cortex's inbox. PUSH with sndhwm: a cortex that is
    // not there must not stall the strip.
    let up = match Sender::connect(&ctx, &args.inbound) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("loa-ring: cannot connect inbound {}: {e}", args.inbound);
            process::exit(1);
        }
    };

    // --- the loop: news in, frames out, on the internal clock --------------
    let mut held = Held::default();
    let mut renderer = Renderer::new();
    let rendered = AtomicU64::new(0);
    let started = Instant::now();
    let mut report_at = Instant::now();
    // The push error line, throttled: a missing cortex must not scream at
    // the frame rate.
    let mut last_push_err = Instant::now() - Duration::from_secs(2);
    let mut suppressed = 0u64;

    loop {
        if let Some(secs) = args.secs {
            if started.elapsed() >= Duration::from_secs(secs) {
                eprintln!(
                    "loa-ring: --secs {secs} up: {} rendered, {} written; exiting clean",
                    rendered.load(Ordering::Relaxed),
                    written.load(Ordering::Relaxed),
                );
                // Skip the socket teardown: a PUSH with unsent frames and
                // default linger blocks `close` forever when the cortex is
                // not there, and a self-test that reports then hangs is
                // worse than one that exits. The kernel closes the fds.
                process::exit(0);
            }
        }
        // Drain everything pending: every event record counts (a one-shot
        // buried by a newer state must still be noted), the newest truth
        // renders.
        loop {
            match sub.recv_nowait() {
                Ok(Some(parts)) => match decode(parts) {
                    Ok(msg) => hold(&mut held, &mut renderer, &msg),
                    Err(e) => eprintln!("loa-ring: feed: {e}"),
                },
                Ok(None) => break,
                Err(e) => {
                    eprintln!("loa-ring: feed: {e}");
                    break;
                }
            }
        }
        // Render uncapped. The frame is the news: strip and cortex both get
        // this exact frame, same bytes.
        let frame = renderer.render(&held.input());
        rendered.fetch_add(1, Ordering::Relaxed);
        if let Some(tx) = &tx {
            let _ = tx.send(frame.rgb);
        }
        let env = envelope(pb::envelope::Body::Ring(pb::Ring {
            ring: Some(frame.rgb.to_vec()),
            ts: Some(wall_now()),
        }));
        if let Err(e) = up.push_frame("ring", &env) {
            if last_push_err.elapsed() >= Duration::from_secs(5) {
                if suppressed > 0 {
                    eprintln!(
                        "loa-ring: push {e} (+{suppressed} suppressed; cortex not listening?)"
                    );
                } else {
                    eprintln!("loa-ring: push {e} (cortex not listening?)");
                }
                last_push_err = Instant::now();
                suppressed = 0;
            } else {
                suppressed += 1;
            }
        }
        report(&rendered, &written, &mut report_at);
        // Park for news, but never longer than a render beat: the breath
        // advances with or without the feed, and its frames are the news.
        tokio::select! {
            r = sub.readable() => {
                if let Err(e) = r {
                    eprintln!("loa-ring: feed: {e}");
                    tokio::time::sleep(Duration::from_millis(100)).await;
                }
            }
            _ = tokio::time::sleep(PARK) => {}
        }
    }
}
