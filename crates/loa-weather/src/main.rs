mod baro;
mod dht;
mod state;

use baro::Baro;
use dht::Dht;
use loa_sense::gpio::{Chip, DEFAULT_CHIP};
use loa_sense::{now_s, secs_from_args, setting, setting_bool, Sense};
use loa_wire::pb;
use state::{Air, News};
use std::sync::{Arc, Mutex};
use std::time::Duration;

#[tokio::main(flavor = "current_thread")]
async fn main() {
    let name = "loa-weather";
    eprintln!(
        "{name}: starting (uplink {}, feed {})",
        loa_sense::inbound_endpoint(),
        loa_sense::feed_endpoint()
    );
    let secs = secs_from_args(name);
    let gpio = setting("LOA_SENSE_TEMP_GPIO", "sense", "temp_gpio", dht::DEFAULT_TEMP_GPIO as f64) as i32;
    let period = setting("LOA_SENSE_TEMP_PERIOD", "sense", "temp_period", dht::DEFAULT_TEMP_PERIOD);
    let bus = setting("LOA_BARO_BUS", "sense", "baro_bus", baro::DEFAULT_BUS as f64) as i32;
    let addr = setting("LOA_SENSE_BARO_ADDR", "sense", "baro_addr", baro::DEFAULT_ADDR as f64) as i32;
    let enabled = setting_bool("LOA_SENSE_BARO_ENABLED", "sense", "baro_enabled", true);
    let dht_on = setting_bool("LOA_SENSE_DHT_ENABLED", "sense", "dht_enabled", true);

    let sense = match Sense::open() {
        Ok(s) => s,
        Err(e) => {
            eprintln!("{name}: cannot open the wire: {e}");
            std::process::exit(1);
        }
    };

    // The pin the DHT lives on. Without it there is no temp/half of the air;
    // the baro could still run, but the reference keeps ONE hardware surface
    // per daemon and exits loud when it is not there (this x86 box: exit 1 is
    // the CORRECT path). LOA_SENSE_DHT_ENABLED=false keeps the baro alone
    // alive for a bench with the DATA line unplugged.
    let chip = if dht_on {
        match Chip::open(DEFAULT_CHIP) {
            Ok(c) => {
                if let Err(e) = c.claim_input(gpio) {
                    eprintln!("{name}: cannot claim DHT GPIO{gpio}: {e}");
                    std::process::exit(1);
                }
                Some(c)
            }
            Err(e) => {
                eprintln!("{name}: no GPIO for the DHT: {e}");
                std::process::exit(1);
            }
        }
    } else {
        None
    };

    let air = Arc::new(Mutex::new(Air::default()));
    {
        let st = Arc::clone(&air);
        sense.on_full_payload(move || pb::envelope::Body::Weather(st.lock().expect("air state").full()));
    }

    // A fresh daemon, a cortex already up: the full payload once at start.
    {
        let st = air.lock().expect("air state");
        let _ = sense.publish(pb::envelope::Body::Weather(st.full()));
    }
    let _ = sense.event(
        "boot",
        &[
            ("svc", "weather"),
            ("temp_gpio", &gpio.to_string()),
            ("temp_period", &format!("{period}")),
            ("baro_enabled", if enabled { "true" } else { "false" }),
            ("baro_addr", &format!("{addr}")),
            ("dht_enabled", if dht_on { "true" } else { "false" }),
        ],
    );
    eprintln!(
        "{name}: DHT GPIO{gpio} every {period}s ({}), baro on /dev/i2c-{bus} 0x{addr:02x} ({}); one Weather payload",
        if dht_on { "on" } else { "off" },
        if enabled { "on" } else { "off" }
    );

    let mut dht = Dht::new(gpio);
    let mut baro = Baro::open(bus, addr);
    let mut baro_fails: u32 = 0;
    let st = Arc::clone(&air);
    sense
        .run_loop(Duration::from_secs_f64(period.max(1.0)), secs, move |sense| {
            // 1. the DHT: the blocking 20ms pulse read, first — it owns the
            // tick's pace.
            if let Some(chip) = chip.as_ref() {
                if let Some((temp, hum)) = dht.read(chip) {
                    let news = st.lock().expect("air state").dht(temp, hum, now_s());
                    apply(news, sense);
                } else if dht.fails == 1 || dht.fails % 10 == 0 {
                    // the reference's log shape: first failure and every
                    // tenth after, so a dead line is visible without flooding
                    // the journal.
                    eprintln!("{name}: dht: read failed ({}x) GPIO{gpio}", dht.fails);
                }
            }
            // 2. the baro: an i2c transaction with two 5ms conversion waits.
            if enabled {
                match baro.read() {
                    Ok((t, pa)) => {
                        baro_fails = 0;
                        let news = st.lock().expect("air state").baro(t, pa / 100.0, now_s());
                        apply(news, sense);
                    }
                    Err(e) => {
                        baro_fails += 1;
                        if baro_fails == 1 || baro_fails % 10 == 0 {
                            eprintln!("{name}: baro: read failed ({baro_fails}x) /dev/i2c-{bus} 0x{addr:02x}: {e}");
                        }
                    }
                }
            }
        })
        .await;
    sense.report(name, secs);
}

fn apply(news: Vec<News>, sense: &Sense) {
    for n in news {
        match n {
            News::Partial(p) => {
                if let Err(e) = sense.publish(pb::envelope::Body::Weather(p)) {
                    eprintln!("loa-weather: push: {e}");
                }
            }
        }
    }
}
