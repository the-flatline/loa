//! loa-matrix — the 8x8 panel, standalone for now.
//!
//! The 3.5" face is the expression; this is the mood grid. It sits on I2C1 at
//! 0x70 alongside the baro at 0x77, so there is no bus of its own to own and
//! no daemon needed to prove it works — this binary is the proof, and the
//! shape the daemon will take once the feed is wired.
//!
//!   loa-matrix scan                what answers on the bus
//!   loa-matrix corner              the orientation test (1/2/3/4 blinks)
//!   loa-matrix fill                every LED on
//!   loa-matrix off                 every LED off
//!   loa-matrix pixel X Y           one LED
//!   loa-matrix box                 the outline, to see the edges
//!
//! `--bright 0-15` on any of them; `--addr`/`--bus` if the panel moves.

mod ht16k33;
mod i2c;

use ht16k33::{Matrix, DEFAULT_ADDR, DEFAULT_BUS};
use std::process::exit;
use std::thread::sleep;
use std::time::Duration;

const USAGE: &str = "\
loa-matrix — the loa 8x8 panel (HT16K33 @ I2C 0x70)

  scan                 list every address that answers
  corner               orientation test: each corner, 1/2/3/4 blinks
  rows [--group 1|2]   which chip address drives which physical column
  bits                 which bit inside the byte drives which physical row
  walk                 one line down the panel, three passes, no counting
  fill                 all 64 LEDs on
  off                  all 64 LEDs off
  pixel X Y            light one LED, 0-7 each, then exit
  box                  the outline, held
  diag                 the diagonal, held (catches a swapped or reversed axis)
  raw IDX VALUE        hold one raw RAM byte (hex, e.g. raw 0e ff) — diagnostics

  --bright N           panel brightness 0-15 (default 2)
  --loops N            corner test passes (default 3)
  --rotate D           clockwise rotation for the coordinate API: 0/90/180/270
  --addr 0xNN          chip address (default 0x70)
  --bus N              I2C bus number (default 1)
";

fn main() -> std::io::Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();

    let mut brightness = 2u8;
    let mut loops = 3u32;
    let mut rotation = 0u16;
    let mut group = 1u8;
    let mut addr = DEFAULT_ADDR;
    let mut bus = DEFAULT_BUS;
    let mut words: Vec<String> = Vec::new();

    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--bright" => {
                brightness = args.get(i + 1).and_then(|v| v.parse().ok()).unwrap_or(2) & 0x0F;
                i += 2;
            }
            "--loops" => {
                loops = args
                    .get(i + 1)
                    .and_then(|v| v.parse().ok())
                    .unwrap_or(3)
                    .max(1);
                i += 2;
            }
            "--rotate" => {
                rotation = args.get(i + 1).and_then(|v| v.parse().ok()).unwrap_or(0);
                i += 2;
            }
            "--group" => {
                group = args.get(i + 1).and_then(|v| v.parse().ok()).unwrap_or(1);
                i += 2;
            }
            "--addr" => {
                let v = args.get(i + 1).map(String::as_str).unwrap_or("0x70");
                addr = u8::from_str_radix(v.trim_start_matches("0x"), 16).unwrap_or(DEFAULT_ADDR);
                i += 2;
            }
            "--bus" => {
                bus = args
                    .get(i + 1)
                    .and_then(|v| v.parse().ok())
                    .unwrap_or(DEFAULT_BUS);
                i += 2;
            }
            "-h" | "--help" => {
                print!("{USAGE}");
                return Ok(());
            }
            other => {
                words.push(other.to_string());
                i += 1;
            }
        }
    }

    let cmd = words.first().map(String::as_str).unwrap_or("");

    if cmd == "scan" {
        match i2c::scan(bus) {
            Ok(found) if found.is_empty() => println!("bus {bus}: nothing answered"),
            Ok(found) => {
                let list: Vec<String> = found.iter().map(|a| format!("0x{a:02x}")).collect();
                println!("bus {bus}: {}", list.join(" "));
            }
            Err(e) => {
                eprintln!("bus {bus}: {e}");
                exit(1);
            }
        }
        return Ok(());
    }

    if cmd.is_empty() {
        print!("{USAGE}");
        exit(2);
    }

    let mut m = match Matrix::open(bus, addr, brightness) {
        Ok(m) => m,
        Err(e) => {
            eprintln!("cannot open panel: {e}");
            eprintln!("check the four leads, then: loa-matrix scan");
            exit(1);
        }
    };

    m.rotation = rotation;

    let result = match cmd {
        "corner" => m.corner_test(Duration::from_millis(450), loops),
        "rows" => m.row_sweep(Duration::from_millis(350), group),
        "bits" => m.bit_sweep(Duration::from_millis(350)),
        "walk" => m.walk(Duration::from_millis(1200), loops),
        "fill" => {
            m.set_all(true);
            m.flush()?;
            sleep(Duration::from_secs(5));
            m.clear();
            m.flush()
        }
        "off" => {
            m.clear();
            m.flush()
        }
        "box" => {
            m.clear();
            for x in 0..8 {
                m.set(x, 0, true);
                m.set(x, 7, true);
            }
            for y in 0..8 {
                m.set(0, y, true);
                m.set(7, y, true);
            }
            m.flush()?;
            sleep(Duration::from_secs(5));
            m.clear();
            m.flush()
        }
        "diag" => {
            m.clear();
            for i in 0..8 {
                m.set(i, i, true);
            }
            m.flush()?;
            sleep(Duration::from_secs(5));
            m.clear();
            m.flush()
        }
        "raw" => {
            let idx = words
                .get(1)
                .and_then(|v| usize::from_str_radix(v.trim_start_matches("0x"), 16).ok())
                .unwrap_or(0);
            let value = words
                .get(2)
                .and_then(|v| u8::from_str_radix(v.trim_start_matches("0x"), 16).ok())
                .unwrap_or(0xFF);
            m.raw(idx, value, Duration::from_secs(4))
        }
        "pixel" => {
            let x: usize = words.get(1).and_then(|v| v.parse().ok()).unwrap_or(0);
            let y: usize = words.get(2).and_then(|v| v.parse().ok()).unwrap_or(0);
            m.clear();
            m.set(x, y, true);
            m.flush()?;
            sleep(Duration::from_secs(5));
            m.clear();
            m.flush()
        }
        other => {
            eprintln!("unknown command: {other}\n");
            print!("{USAGE}");
            exit(2);
        }
    };

    if let Err(e) = result {
        eprintln!("panel write failed: {e}");
        exit(1);
    }

    // Leave the panel dark rather than frozen on the last test frame: a lit
    // panel on the bench looks like a working panel, and this one is not yet.
    let _ = m.display(true);
    Ok(())
}
