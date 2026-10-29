//! loa-weather — the environment daemon. Two sources, two topic names.
//!
//! STATUS: the BARO is ported; the DHT is not. This binary is deliberately NOT
//! wired to loa-weather.service yet: the Python daemon hosts both sources, and
//! swapping early would take temp_c/hum_pct off the feed while the DHT half is
//! still being written. `--once` exists so the baro can be checked against the
//! Python driver's own numbers before anything is swapped.
//!
//! The daemon hosts TWO topics (weather + baro) the way the Python one did, and
//! the init answer re-sends each under its own name — the same split the
//! readings arrive under.
mod baro;

use loa_sense::{pause, setting, setting_bool, Sense};
use loa_topic::pb;
use std::sync::{Arc, Mutex};

const DEFAULT_BARO_ADDR: i32 = 0x77;
const DEFAULT_BARO_PERIOD: f64 = 10.0;
const DEFAULT_TEMP_PERIOD: f64 = 10.0;
const DEFAULT_TEMP_GPIO: i32 = 4;

fn main() {
    let bus = setting("LOA_BARO_BUS", "sense", "baro_bus", 1.0) as i32;
    let addr = setting("LOA_SENSE_BARO_ADDR", "sense", "baro_addr",
                       DEFAULT_BARO_ADDR as f64) as i32;
    let period = setting("LOA_SENSE_BARO_PERIOD", "sense", "baro_period",
                         DEFAULT_BARO_PERIOD);
    let enabled = setting_bool("LOA_SENSE_BARO_ENABLED", "sense", "baro_enabled", true);

    if std::env::args().any(|a| a == "--once") {
        let mut b = baro::Baro::open(bus, addr);
        match b.read() {
            Ok((t, pa)) => {
                println!("baro: temp_c = {t:.4}   pa = {pa:.0}   ({:.2} hPa)",
                         pa / 100.0);
                return;
            }
            Err(e) => {
                eprintln!("baro: read failed: {e}");
                std::process::exit(1);
            }
        }
    }

    let sense = match Sense::open(loa_sense::DEFAULT_INBOUND) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("loa-weather: no uplink: {e}");
            std::process::exit(1);
        }
    };

    let _ = sense.event("boot", &[
        ("svc", "weather"),
        ("baro_enabled", if enabled { "true" } else { "false" }),
        ("baro_addr", &format!("{addr}")),
        ("baro_period", &format!("{period}")),
    ]);

    // What this daemon remembers for the init handshake: the last reading, so a
    // restarted cortex hears the air rather than waiting for the next poll.
    let last: Arc<Mutex<(Option<f64>, Option<f64>, Option<i32>)>> =
        Arc::new(Mutex::new((None, None, None)));

    if enabled {
        let answer = {
            let st = Arc::clone(&last);
            move || {
                let g = st.lock().expect("state");
                pb::envelope::Body::Baro(pb::Baro {
                    pressure_hpa: g.0,
                    temp_c: g.1,
                    count: g.2.map(|c| c as u32),
                    ..Default::default()
                })
            }
        };
        if let Err(e) = sense.answer_init(loa_sense::DEFAULT_FEED, answer) {
            eprintln!("loa-weather: no init listener: {e}");
        }
    }

    eprintln!("loa-weather: baro on /dev/i2c-{bus} 0x{addr:02x} every {period}s, \
               DHT GPIO{DEFAULT_TEMP_GPIO} {DEFAULT_TEMP_PERIOD}s (NOT YET PORTED)");

    let mut b = baro::Baro::open(bus, addr);
    let mut count: i32 = 0;
    let mut fails: u32 = 0;
    loop {
        if enabled {
            match b.read() {
                Ok((t, pa)) => {
                    fails = 0;
                    count += 1;
                    let hpa = pa / 100.0;
                    {
                        let mut g = last.lock().expect("state");
                        *g = (Some(hpa), Some(t), Some(count));
                    }
                    let _ = sense.send(pb::envelope::Body::Baro(pb::Baro {
                        pressure_hpa: Some(hpa),
                        temp_c: Some(t),
                        ts: Some(loa_sense::now()),
                        count: Some(count as u32),
                        ..Default::default()
                    }));
                }
                Err(e) => {
                    fails += 1;
                    // First failure and every tenth after, as the Python did: a
                    // dead chip must be visible without flooding the journal.
                    if fails == 1 || fails % 10 == 0 {
                        eprintln!("baro: read failed ({fails}x) /dev/i2c-{bus} \
                                   0x{addr:02x}: {e}");
                    }
                }
            }
        }
        pause(period);
    }
}
