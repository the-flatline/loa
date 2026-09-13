//! loa-ring — the ring display, in Rust. Owns the WS2812B over SPI1.
//!
//! A PURE DISPLAY, same contract as the Python daemon it replaces: subscribe
//! to the `ring` topic and blit the 72 bytes the cortex rendered. It does not
//! render, derive state, or push anything up. The tell — alarm > hurts > mute >
//! busy > one-shot > home — is decided in the brain, and the brain publishes
//! the pixels.
//!
//!    loa-ring                the daemon (runs forever)
//!    loa-ring --secs 10      run for 10 seconds and report what it wrote
//!
//! The ring is the body's INVOLUNTARY tell — the ears and the tail — and it
//! still works when the face cannot. That is a property of the tell, not of
//! this process.

mod neopixel;

use std::time::{Duration, Instant};

use loa_topic::{feed_endpoints, Subscriber};
use neopixel::Ring;

fn arg(name: &str, default: &str) -> String {
    let argv: Vec<String> = std::env::args().collect();
    let mut i = 1;
    while i < argv.len() {
        if argv[i] == name {
            if let Some(v) = argv.get(i + 1) {
                return v.clone();
            }
        }
        i += 1;
    }
    default.to_string()
}

fn main() {
    let secs: f64 = arg("--secs", "0").parse().unwrap_or(0.0);
    let spi = arg("--spi", "/dev/spidev1.0");
    let hz: u32 = arg("--hz", &neopixel::DEFAULT_HZ.to_string())
        .parse()
        .unwrap_or(neopixel::DEFAULT_HZ);

    let mut ring = match Ring::open(&spi, hz) {
        Ok(r) => r,
        Err(e) => {
            eprintln!("loa-ring: no ring on {spi}: {e}");
            std::process::exit(1);
        }
    };
    // A ring must never sit on the last process's frozen colour.
    let _ = ring.off();

    let endpoints = feed_endpoints();
    let mut sub = match Subscriber::connect(&endpoints, &["ring"]) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("loa-ring: no feed at {endpoints:?}: {e}");
            std::process::exit(1);
        }
    };

    let mut written = 0u64;
    let mut repeated = 0u64;
    let mut last: Vec<u8> = Vec::new();
    let mut since = Instant::now();
    let mut blit_ns = 0u128;
    let started = Instant::now();

    loop {
        if secs > 0.0 && started.elapsed().as_secs_f64() >= secs {
            break;
        }
        // Keep the LAST frame of a batch: a ring replaying a backlog shows a
        // colour the body has already left behind.
        let batch = match sub.drain() {
            Ok(b) => b,
            Err(e) => {
                eprintln!("loa-ring: feed error: {e}");
                std::thread::sleep(Duration::from_millis(50));
                continue;
            }
        };
        let empty = batch.is_empty();
        for msg in batch {
            let Some(m) = msg.ring() else { continue };
            let Some(raw) = m.ring.as_deref() else { continue };
            if raw.len() != neopixel::BYTES || raw == last.as_slice() {
                repeated += 1;
                continue;
            }
            let t0 = Instant::now();
            if let Err(e) = ring.show(raw) {
                eprintln!("loa-ring: blit failed: {e}");
            }
            blit_ns += t0.elapsed().as_nanos();
            last.clear();
            last.extend_from_slice(raw);
            written += 1;
        }
        if since.elapsed() >= Duration::from_secs(5) {
            let el = since.elapsed().as_secs_f64();
            eprintln!(
                "loa-ring: {:.0} written/s, {:.0} identical/s, {:.3} ms/blit",
                written as f64 / el,
                repeated as f64 / el,
                if written > 0 {
                    blit_ns as f64 / written as f64 / 1e6
                } else {
                    0.0
                }
            );
            written = 0;
            repeated = 0;
            blit_ns = 0;
            since = Instant::now();
        }
        if empty {
            std::thread::sleep(Duration::from_millis(1));
        }
    }
    let _ = ring.off();
    eprintln!("loa-ring: done after {:.1}s", started.elapsed().as_secs_f64());
}
