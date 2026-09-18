//! The face daemon. State down, frames up.
//!
//! Subscribes to the topics it draws — state, pir, sonar, weather, fault,
//! power — holds the last message of each, and RENDERS when any of them
//! moves. There is no tick in this system: news only. The 1024 bytes go two
//! places from here: to the SPI glass, and back UP to the cortex on the
//! inbound PUSH as topic `oled` (the cortex relays oled->ripperdoc for
//! consumers). The daemon never subscribes to `ripperdoc` — that would be an
//! echo of its own output.
//!
//! The SPI write is ~2ms of blocking syscalls, so ONE thread owns the Oled
//! and reads frames off a channel; the reactor never touches a syscall.
//! Every rendered frame is pushed as fast as it is produced — the 120fps cap
//! is the panel's, not this code's — and `--fps N` trims the push rate if
//! anyone ever needs it.
//!
//! No glass (this bench box, a build farm): the process still starts, the
//! render path still runs, the frames still go up. `Oled::open` failing is a
//! clear error at runtime, not a compile error off-Pi.

use std::process;
use std::sync::mpsc;
use std::time::{Duration, Instant};

use loa_wire::{aio, envelope, pb, Sender};

use loa_oled::oled::Oled;
use loa_oled::render::{FaceRenderer, WASH_EVERY_S};
use loa_oled::rstate::RenderState;

/// The topics the face draws FROM. Never `ripperdoc`: that is the face's own
/// output relayed back by the cortex.
const TOPICS: [&str; 6] = ["state", "pir", "sonar", "weather", "fault", "power"];

struct Args {
    feed: String,
    inbound: String,
    spi: String,
    speed_hz: u32,
    gpio_chip: i32,
    secs: Option<u64>,
    fps: Option<f64>,
    /// --no-glass: do not open SPI even if it is there (bench on a Pi with
    /// the panel wired to another process).
    no_glass: bool,
}

fn usage() -> ! {
    eprintln!(
        "usage: loa-oled [--feed HOST] [--inbound URL] [--spi PATH]\n\
         \x20             [--speed HZ] [--chip N] [--secs N] [--fps N] [--no-glass]"
    );
    process::exit(2);
}

fn parse_args() -> Args {
    let mut a = Args {
        // The cortex's PUB. LOA_TOPIC_ENDPOINT names the host (the wire's
        // convention) or spells a full endpoint itself; the feed port is the
        // contract (there is no feed_endpoints helper any more).
        feed: match std::env::var("LOA_TOPIC_ENDPOINT") {
            Ok(v) if v.contains("://") => v,
            Ok(host) => format!("tcp://{host}:{}", loa_wire::FEED_PORT),
            Err(_) => format!("tcp://127.0.0.1:{}", loa_wire::FEED_PORT),
        },
        inbound: std::env::var("LOA_INBOUND")
            .unwrap_or_else(|_| format!("tcp://127.0.0.1:{}", loa_wire::INBOUND_PORT)),
        spi: std::env::var("LOA_SPI").unwrap_or_else(|_| "/dev/spidev0.0".into()),
        speed_hz: std::env::var("LOA_OLED_SPEED")
            .ok()
            .and_then(|s| s.parse().ok())
            .unwrap_or(500_000),
        gpio_chip: std::env::var("LOA_OLED_CHIP")
            .ok()
            .and_then(|s| s.parse().ok())
            .unwrap_or(0),
        secs: None,
        fps: None,
        no_glass: false,
    };
    let argv: Vec<String> = std::env::args().collect();
    fn take(argv: &[String], i: &mut usize, name: &str) -> String {
        *i += 1;
        argv.get(*i).cloned().unwrap_or_else(|| {
            eprintln!("loa-oled: {name} wants a value");
            process::exit(2);
        })
    }
    let mut i = 1;
    while i < argv.len() {
        match argv[i].as_str() {
            "--feed" => a.feed = take(&argv, &mut i, "--feed"),
            "--inbound" => a.inbound = take(&argv, &mut i, "--inbound"),
            "--spi" => a.spi = take(&argv, &mut i, "--spi"),
            "--speed" => a.speed_hz = take(&argv, &mut i, "--speed").parse().unwrap_or_else(|_| usage()),
            "--chip" => a.gpio_chip = take(&argv, &mut i, "--chip").parse().unwrap_or_else(|_| usage()),
            "--secs" => a.secs = Some(take(&argv, &mut i, "--secs").parse().unwrap_or_else(|_| usage())),
            "--fps" => a.fps = Some(take(&argv, &mut i, "--fps").parse().unwrap_or_else(|_| usage())),
            "--no-glass" => a.no_glass = true,
            "--help" | "-h" => usage(),
            other => {
                eprintln!("loa-oled: not an argument I know: {other}");
                usage();
            }
        }
        i += 1;
    }
    a
}

/// One item for the thread that owns the glass. A bounded queue: the newest
/// frame is the only one worth drawing, and a queue that grows is a queue
/// that draws the past.
struct Blit {
    buf: Vec<u8>,
    dim: bool,
    flip: bool,
}

/// The glass thread. Blocking by design — DC/RES + SPI are syscalls, and a
/// reactor must not park on them. Returns the counts so --secs can report.
fn glass_worker(
    mut oled: Oled,
    rx: mpsc::Receiver<Blit>,
    written: &std::sync::atomic::AtomicU64,
) {
    use std::sync::atomic::Ordering;
    // A panel must never sit on the last process's frozen frame.
    if let Err(e) = oled.clear() {
        eprintln!("loa-oled: clear: {e}");
    }
    while let Ok(b) = rx.recv() {
        let contrast = if b.dim {
            loa_oled::oled::DIM
        } else {
            loa_oled::oled::BRIGHT
        };
        // The reference's dedupe carried over: identical bytes AND contrast
        // AND flip is work no one can see. The flip/contrast check is the
        // panel driver's own (it remembers its registers), so only the frame
        // compare lives here.
        if let Err(e) = oled.set_flip(b.flip) {
            eprintln!("loa-oled: flip: {e}");
            continue;
        }
        if let Err(e) = oled.set_contrast(contrast) {
            eprintln!("loa-oled: contrast: {e}");
        }
        if let Err(e) = oled.show(&b.buf) {
            eprintln!("loa-oled: show: {e}");
            continue;
        }
        written.fetch_add(1, Ordering::Relaxed);
    }
}

/// The rate line the old daemon printed every 5s: what reached the glass,
/// what was produced, and the render clock the scope lives on.
fn report(produced: &std::sync::atomic::AtomicU64, written: &std::sync::atomic::AtomicU64, at: &mut Instant) {
    use std::sync::atomic::Ordering;
    let el = at.elapsed().as_secs_f64();
    if el >= 5.0 {
        eprintln!(
            "loa-oled: {:.0} produced/s, {:.0} written/s (the panel only sees what it was not shown last)",
            produced.swap(0, Ordering::Relaxed) as f64 / el,
            written.swap(0, Ordering::Relaxed) as f64 / el,
        );
        *at = Instant::now();
    }
}

#[tokio::main]
async fn main() {
    let args = parse_args();

    // --- the glass: one blocking thread, one owner ------------------------
    let (tx, rx) = mpsc::channel::<Blit>();
    let written = std::sync::Arc::new(std::sync::atomic::AtomicU64::new(0));
    let glass = if args.no_glass {
        eprintln!("loa-oled: --no-glass: frames render and ride up, nothing blits");
        None
    } else {
        match Oled::open(&args.spi, args.speed_hz, args.gpio_chip) {
            Ok(o) => Some(o),
            Err(e) => {
                // Off-Pi this is the expected story; the daemon must still
                // feed the cortex. The glass is a choice, the frame is the job.
                eprintln!("loa-oled: no panel on {}: {e} — rendering without it", args.spi);
                None
            }
        }
    };
    if let Some(oled) = glass {
        let counter = std::sync::Arc::clone(&written);
        std::thread::spawn(move || glass_worker(oled, rx, &counter));
    }

    // --- the wire ----------------------------------------------------------
    let ctx = zmq::Context::new();
    let sub = match aio::AInbound::sub(&ctx, &[args.feed.clone()], &TOPICS) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("loa-oled: cannot subscribe to {}: {e}", args.feed);
            process::exit(1);
        }
    };
    // The out-leg to the cortex's inbox. PUSH with sndhwm: a cortex that is
    // not there must not stall the glass.
    let up = match Sender::connect(&ctx, &args.inbound) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("loa-oled: cannot connect inbound {}: {e}", args.inbound);
            process::exit(1);
        }
    };

    // --- the renderer: one instance, its own clocks ------------------------
    let now = || {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0)
    };
    let mut held = RenderState::default();
    let mut renderer = FaceRenderer::new(now());
    let produced = std::sync::Arc::new(std::sync::atomic::AtomicU64::new(0));

    let started = Instant::now();
    let mut report_at = Instant::now();
    let mut gate = Gate::new(args.fps);
    // The wash must happen even when nothing is news: it is a clock rule, and
    // a daemon that renders only on change would sleep straight through it.
    // The wake is the wash edge minus a frame's slack, recomputed after each.
    let mut next_wake = Duration::from_secs_f64((WASH_EVERY_S * 2.0).min(1.0));

    loop {
        if let Some(secs) = args.secs {
            if started.elapsed() >= Duration::from_secs(secs) {
                eprintln!("loa-oled: --secs {secs} up; exiting clean");
                return;
            }
        }
        tokio::select! {
            r = sub.next_state() => {
                match r {
                    Ok(msg) => {
                        held.hold(&msg.topic, &msg.env);
                        emit(&mut renderer, &mut held, &tx, &up, &produced, &mut gate, now());
                    }
                    Err(e) => eprintln!("loa-oled: feed: {e}"),
                }
            }
            _ = tokio::time::sleep(next_wake) => {
                // The only timer in the daemon: it exists to keep the wash
                // honest, not to animate. If nothing moved, nothing redraws
                // beyond the wake itself.
                let t = now();
                if t >= renderer.next_wash {
                    emit(&mut renderer, &mut held, &tx, &up, &produced, &mut gate, t);
                }
            }
        }
        next_wake = {
            let t = now();
            let edge = renderer.next_wash.max(t);
            Duration::from_secs_f64(((edge - t) + 0.2).clamp(0.05, 5.0))
        };
        report(&produced, &written, &mut report_at);
    }
}

/// Render now and send the result wherever it goes: the glass thread and the
/// cortex. `gate` trims the UPSTREAM push when --fps asked for it; the render
/// and the blit stay uncapped because a held-back frame is a frozen glass.
fn emit(
    renderer: &mut FaceRenderer,
    held: &mut RenderState,
    tx: &mpsc::Sender<Blit>,
    up: &Sender,
    produced: &std::sync::atomic::AtomicU64,
    gate: &mut Gate,
    t: f64,
) {
    use std::sync::atomic::Ordering;
    let buf = renderer.render(held, t);
    produced.fetch_add(1, Ordering::Relaxed);
    // The glass: hand off, never wait on a syscall from the reactor.
    let _ = tx.send(Blit {
        buf: buf.to_vec(),
        dim: held.dim,
        flip: held.flip,
    });
    // Up: the finished pixels + the ts, nothing else. The cortex fills the
    // state legs from its own held view and relays under `ripperdoc`.
    if gate.allows(t) {
        let env = envelope(pb::envelope::Body::Ripperdoc(pb::Ripperdoc {
            face: Some(buf.to_vec()),
            ts: Some(t),
            ..Default::default()
        }));
        if let Err(e) = up.push_frame("oled", &env) {
            eprintln!("loa-oled: push: {e}");
        }
    }
}

/// The optional --fps trim on the upstream push. None = as fast as produced.
struct Gate {
    every: Option<f64>,
    last: f64,
}

impl Gate {
    fn new(fps: Option<f64>) -> Self {
        Gate { every: fps.filter(|f| *f > 0.0).map(|f| 1.0 / f), last: 0.0 }
    }
    fn allows(&mut self, t: f64) -> bool {
        match self.every {
            None => true,
            Some(iv) if t - self.last >= iv => {
                self.last = t;
                true
            }
            Some(_) => false,
        }
    }
}
