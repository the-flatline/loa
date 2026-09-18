//! loa-motion — the PIR daemon, in Rust. The body's eye, on GPIO17.
//!
//! The Python daemon polled the pin by SPAWNING `pinctrl get 17` five times a
//! second — the same fork-per-read disease the panel had at 16 forks a frame.
//! This reads the line through liblgpio (the history's layer, carried forward
//! in loa-sense::gpio): no process, no parsing, and a read that cannot be
//! misread as a level when it is really an error.
//!
//! The state machine — edges, the pending>=2 debounce, the cooldown, the
//! partial-versus-full payloads — lives in `state` and is tested without a
//! pin. This file is the pin and the loop.
mod state;

use loa_sense::gpio::{Chip, DEFAULT_CHIP};
use loa_sense::{now_s, secs_from_args, setting, Sense};
use loa_wire::pb;
use state::{News, Pir};
use std::sync::{Arc, Mutex};
use std::time::Duration;

const POLL_PERIOD: f64 = 0.2;
const DEFAULT_GPIO: f64 = 17.0;
const DEFAULT_COOLDOWN: f64 = 5.0;

// current_thread: the liblgpio Chip is dlopen'd and !Send, and it is held
// across the loop's awaits. Nothing here wants a second worker anyway — one
// poll every 200ms is the whole schedule.
#[tokio::main(flavor = "current_thread")]
async fn main() {
    let name = "loa-motion";
    eprintln!(
        "{name}: starting (uplink {}, feed {})",
        loa_sense::inbound_endpoint(),
        loa_sense::feed_endpoint()
    );
    let secs = secs_from_args(name);
    let gpio = setting("LOA_SENSE_GPIO", "sense", "gpio", DEFAULT_GPIO) as i32;
    let cooldown = setting("LOA_SENSE_COOLDOWN", "sense", "cooldown", DEFAULT_COOLDOWN);

    let sense = match Sense::open() {
        Ok(s) => s,
        Err(e) => {
            eprintln!("{name}: cannot open the wire: {e}");
            std::process::exit(1);
        }
    };

    let chip = match Chip::open(DEFAULT_CHIP) {
        Ok(c) => c,
        Err(e) => {
            // Without the pin there is no sense to be. Say so and stop: a
            // daemon that keeps running without its sensor looks alive on the
            // feed. On a box with no lgpio this is the correct exit.
            eprintln!("{name}: no GPIO: {e}");
            std::process::exit(1);
        }
    };
    if let Err(e) = chip.claim_input(gpio) {
        eprintln!("{name}: cannot claim GPIO{gpio}: {e}");
        std::process::exit(1);
    }

    // The init answer comes from this memory; the poll cursor does not.
    let pir = Arc::new(Mutex::new(Pir::new(cooldown)));
    {
        let st = Arc::clone(&pir);
        sense.on_full_payload(move || pb::envelope::Body::Pir(st.lock().expect("pir state").full()));
    }

    // A fresh daemon, a cortex already up: the full payload once at start.
    // The counter is per-boot, so it starts at zero and says so.
    {
        let st = pir.lock().expect("pir state");
        let _ = sense.publish(pb::envelope::Body::Pir(st.full()));
    }
    let _ = sense.event(
        "boot",
        &[
            ("svc", "motion"),
            ("gpio", &gpio.to_string()),
            ("cooldown", &format!("{cooldown}")),
        ],
    );
    // The current pin level on our own start — the other half of the
    // handshake: whenever this process comes up, the cortex hears everything
    // rather than a change against a state it does not have.
    if let Ok(level) = chip.read(gpio) {
        let m = pir.lock().expect("pir state").seed(level != 0, now_s());
        let _ = sense.publish(pb::envelope::Body::Pir(m));
    }

    eprintln!("loa-motion: GPIO{gpio}, cooldown {cooldown}s, poll {POLL_PERIOD}s, uplink {}", sense.inbound);

    let st = Arc::clone(&pir);
    sense
        .run_loop(Duration::from_secs_f64(POLL_PERIOD), secs, move |sense| {
            let level = match chip.read(gpio) {
                Ok(v) => v != 0,
                Err(_) => {
                    // The pin stopped reporting. Re-assert the input on the
                    // next pass rather than believing a level that was not
                    // read; the pending streak resets with the cursor that
                    // did not observe it.
                    let _ = chip.claim_input(gpio);
                    st.lock().expect("pir state").reset_pending();
                    return;
                }
            };
            let news = st.lock().expect("pir state").tick(level, now_s());
            for n in news {
                match n {
                    News::State(m) => {
                        if let Err(e) = sense.publish(pb::envelope::Body::Pir(m)) {
                            eprintln!("{name}: push: {e}");
                        }
                    }
                    News::Fire { msg, count } => {
                        if let Err(e) = sense.publish(pb::envelope::Body::Pir(msg)) {
                            eprintln!("{name}: push: {e}");
                        }
                        let g = gpio.to_string();
                        let c = cooldown.to_string();
                        let n = count.to_string();
                        let _ = sense.event(
                            "sense",
                            &[
                                ("kind", "pir"),
                                ("gpio", &g),
                                ("action", "motion"),
                                ("count", &n),
                                ("cooldown", &c),
                            ],
                        );
                        // A scan is a one-shot RECORD, not a flag in another
                        // service's state: the ring hears it on the event
                        // topic and plays it.
                        let _ = sense.event("ring", &[("state", "scan")]);
                    }
                }
            }
        })
        .await;
    sense.report(name, secs);
}
