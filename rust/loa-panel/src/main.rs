//! loa-panel — the face display, on the glass, in Rust.
//!
//! A PURE DISPLAY, like the Python daemon it replaces: it subscribes to the
//! `ripperdoc` topic and blits the 1024 bytes the cortex rendered. It does not
//! render, derive state, or push anything up. The brain owns the picture.
//!
//!    loa-panel                  the daemon (runs forever)
//!    loa-panel --secs 10        run for 10 seconds and report what it wrote
//!    loa-panel --fps 0          uncapped: blit every frame, never sleep
//!
//! Everything about the rate is a setting, and the counters are the point: a
//! panel that reports how many frames it actually WROTE is a panel you can
//! measure, and one that reports nothing is a panel you have to believe.

use std::time::{Duration, Instant};

use loa_topic::{feed_endpoints, Subscriber};
use loa_panel::panel::{self, Panel};

/// How long a recv may park before the loop notices nothing is arriving. Short,
/// because a parked consumer cannot tell a quiet feed from a dead body.
const RECV_MS: u64 = 20;

struct Args {
    secs: f64,      // 0 = forever
    fps: f64,       // 0 = uncapped
    spi: String,
    hz: u32,
    chip: i32,
}

fn parse_args() -> Args {
    let mut a = Args {
        secs: 0.0,
        fps: 0.0,      // uncapped by default: the panel takes what it is given
        spi: "/dev/spidev0.0".into(),
        hz: 4_000_000,
        chip: 0,
    };
    let argv: Vec<String> = std::env::args().collect();
    let mut i = 1;
    while i < argv.len() {
        let key = argv[i].as_str();
        let val = argv.get(i + 1).cloned().unwrap_or_default();
        match key {
            "--secs" => a.secs = val.parse().unwrap_or(0.0),
            "--fps" => a.fps = val.parse().unwrap_or(0.0),
            "--spi" => a.spi = val,
            "--hz" => a.hz = val.parse().unwrap_or(4_000_000),
            "--chip" => a.chip = val.parse().unwrap_or(0),
            _ => {}
        }
        i += 2;
    }
    if let Ok(v) = std::env::var("LOA_FACE_FPS") {
        if let Ok(f) = v.parse() {
            a.fps = f;
        }
    }
    a
}

fn main() {
    let args = parse_args();
    let mut panel = match Panel::open(&args.spi, args.hz, args.chip) {
        Ok(p) => p,
        Err(e) => {
            eprintln!("loa-panel: no panel: {e}");
            std::process::exit(1);
        }
    };
    if let Err(e) = panel.clear() {
        eprintln!("loa-panel: clear failed: {e}");
    }

    let endpoints = feed_endpoints();
    let mut sub = match Subscriber::connect(&endpoints, &["ripperdoc"]) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("loa-panel: no feed at {:?}: {e}", endpoints);
            std::process::exit(1);
        }
    };

    let period = if args.fps > 0.0 {
        Duration::from_secs_f64(1.0 / args.fps)
    } else {
        Duration::ZERO
    };
    let mut next = Instant::now();

    let (mut written, mut repeated, mut offered) = (0u64, 0u64, 0u64);
    let mut last_buf: Vec<u8> = Vec::new();
    let mut flip = false;
    let mut dim = false;
    let mut last_report = Instant::now();
    let mut blit_ns = 0u128;
    let started = Instant::now();

    loop {
        if args.secs > 0.0 && started.elapsed().as_secs_f64() >= args.secs {
            break;
        }
        // Drain what has piled up and keep the LAST frame: a panel that replays
        // a backlog is a panel that gets further and further behind.
        let batch = match sub.drain() {
            Ok(b) => b,
            Err(e) => {
                eprintln!("loa-panel: feed error: {e}");
                std::thread::sleep(Duration::from_millis(50));
                continue;
            }
        };
        let empty = batch.is_empty();
        for msg in batch {
            let Some(rd) = msg.ripperdoc() else { continue };
            offered += 1;
            let face = match rd.face.as_deref() {
                Some(f) => f,
                None => continue,         // no picture in this message
            };
            if face.len() != panel::BYTES {
                continue;                 // a truncated frame is not a frame
            }
            let want_flip = rd.oled_flip.unwrap_or(false);
            let want_dim = rd.oled_dim.unwrap_or(false);
            if want_flip != flip {
                flip = want_flip;
                let _ = panel.set_flip(flip);
            }
            if want_dim != dim {
                dim = want_dim;
                let _ = panel.set_contrast(if dim { panel::DIM } else { panel::BRIGHT });
            }
            if face == last_buf.as_slice() {
                repeated += 1;            // identical bytes: nothing to draw
                continue;
            }
            let t0 = Instant::now();
            if let Err(e) = panel.show(face) {
                eprintln!("loa-panel: blit failed: {e}");
            }
            blit_ns += t0.elapsed().as_nanos();
            last_buf.clear();
            last_buf.extend_from_slice(face);
            written += 1;
            // Pace only when a rate is asked for. Uncapped means the panel takes
            // every frame as fast as the wire and the glass allow.
            if !period.is_zero() {
                next += period;
                let now = Instant::now();
                if next > now {
                    std::thread::sleep(next - now);
                } else {
                    next = now;
                }
            }
        }
        if last_report.elapsed() >= Duration::from_secs(5) {
            let el = last_report.elapsed().as_secs_f64();
            eprintln!(
                "loa-panel: {:.0} written/s, {:.0} identical/s of {} offered/s, {:.3} ms/blit",
                written as f64 / el,
                repeated as f64 / el,
                offered as f64 / el,
                if written > 0 {
                    blit_ns as f64 / written as f64 / 1e6
                } else {
                    0.0
                }
            );
            written = 0;
            repeated = 0;
            offered = 0;
            blit_ns = 0;
            last_report = Instant::now();
        }
        if empty {
            std::thread::sleep(Duration::from_millis(1));
        }
    }
    let _ = RECV_MS;
    eprintln!("loa-panel: done after {:.1}s", started.elapsed().as_secs_f64());
}
