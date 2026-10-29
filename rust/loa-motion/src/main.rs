//! loa-motion — the PIR daemon, in Rust. The body's eye, on GPIO17.
//!
//! The Python daemon polled the pin by SPAWNING `pinctrl get 17` five times a
//! second — the same fork-per-read disease the panel had at 16 forks a frame.
//! This reads the line through liblgpio: no process, no parsing, and a read that
//! cannot be misread as a level when it is really an error.
//!
//! WHAT A READING IS. The Python side publishes PARTIAL messages — only the
//! fields a transition touched — and the cortex merges by PRESENCE, so a field
//! left out keeps its old value. Sending the whole struct every time would look
//! equivalent and is not: it would start clobbering fields this daemon did not
//! measure. So the partials are mirrored here exactly, and the accumulated
//! union is kept for the init answer (`republish_full` in the Python).
use std::sync::{Arc, Mutex};

use loa_gpio::{Chip, DEFAULT_CHIP};
use loa_sense::{now, pause, setting, Sense};
use loa_topic::pb;

const POLL_PERIOD: f64 = 0.2;
const DEFAULT_GPIO: f64 = 17.0;
const DEFAULT_COOLDOWN: f64 = 5.0;

/// What this daemon has published so far. It is the daemon's OWN memory: the
/// Python version used to ask the cortex for the count it had itself published.
#[derive(Default)]
struct Remembered {
    high: bool,
    count: u32,
    last_ts: Option<f64>,
    last_hold: Option<f64>,
    on_ts: Option<f64>,
}

impl Remembered {
    /// The full payload: what an init answer carries.
    fn full(&self) -> pb::Pir {
        pb::Pir {
            high: Some(self.high),
            count: Some(self.count),
            last_ts: self.last_ts,
            last_hold: self.last_hold,
            // Only ever set by a rise, exactly as in Python: the falling edge
            // published `on_ts: None`, which `partial` drops rather than sends.
            on_ts: self.on_ts,
            ts: None,
        }
    }
}

struct Poller {
    gpio: i32,
    cooldown: f64,
    sense: Arc<Sense>,
    /// SHARED with the init thread; the poller's own cursor (last, pending,
    /// last_fire, on_ts) is NOT, because nothing but this loop may touch it.
    /// The first version shared the whole Poller and the loop then panicked
    /// trying to borrow it mutably past the init thread's handle.
    state: Arc<Mutex<Remembered>>,
    last: bool,
    pending: u32,
    last_fire: f64,
    on_ts: Option<f64>,
}

impl Poller {
    fn remember(&self, patch: impl FnOnce(&mut Remembered)) {
        let mut st = self.state.lock().expect("state");
        patch(&mut st);
    }

    fn publish(&self, msg: pb::Pir) -> Result<(), String> {
        self.sense.send(pb::envelope::Body::Pir(msg))
    }

    /// One poll. Returns the level it saw, or None when the pin would not read.
    fn tick(&mut self, chip: &Chip) -> Option<bool> {
        let level = match chip.read(self.gpio) {
            Ok(v) => v != 0,
            Err(_) => {
                // The pin stopped reporting. Re-assert the input on the next
                // pass rather than believing a level that was not read.
                let _ = chip.claim_input(self.gpio);
                self.pending = 0;
                return None;
            }
        };
        let now = now();
        if level != self.last {
            if level {
                self.on_ts = Some(now);
                self.remember(|s| s.high = true);
                let _ = self.publish(pb::Pir {
                    high: Some(true),
                    on_ts: Some(now),
                    ..Default::default()
                });
            } else {
                let hold = self.on_ts.map(|t| now - t).unwrap_or(0.0);
                self.on_ts = None;
                self.remember(|s| {
                    s.high = false;
                    s.last_hold = Some(hold);
                });
                let _ = self.publish(pb::Pir {
                    high: Some(false),
                    last_hold: Some(hold),
                    ..Default::default()
                });
            }
        }
        if level {
            self.pending = if self.last { self.pending + 1 } else { 1 };
            if self.pending >= 2 && now - self.last_fire >= self.cooldown {
                self.last_fire = now;
                self.fire(now);
            }
        } else {
            self.pending = 0;
        }
        self.last = level;
        Some(level)
    }

    fn fire(&self, now: f64) {
        let n = {
            let mut st = self.state.lock().expect("state");
            st.count += 1;
            st.last_ts = Some(now);
            st.count
        };
        let _ = self.publish(pb::Pir {
            count: Some(n),
            last_ts: Some(now),
            ..Default::default()
        });
        let gpio = self.gpio.to_string();
        let cooldown = format!("{}", self.cooldown);
        let count = n.to_string();
        let _ = self.sense.event(
            "sense",
            &[
                ("kind", "pir"),
                ("gpio", &gpio),
                ("action", "motion"),
                ("count", &count),
                ("cooldown", &cooldown),
            ],
        );
        // A scan is a one-shot RECORD, not a flag in another service's state:
        // the ring hears it on the event topic and plays it.
        let _ = self.sense.event("ring", &[("state", "scan")]);
    }
}

fn main() {
    let gpio = setting("LOA_SENSE_GPIO", "sense", "gpio", DEFAULT_GPIO) as i32;
    let cooldown = setting("LOA_SENSE_COOLDOWN", "sense", "cooldown", DEFAULT_COOLDOWN);

    let sense = match Sense::open(loa_sense::DEFAULT_INBOUND) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("loa-motion: no uplink to the cortex: {e}");
            std::process::exit(1);
        }
    };

    let chip = match Chip::open(DEFAULT_CHIP) {
        Ok(c) => c,
        Err(e) => {
            // Without the pin there is no sense to be. Say so and stop: a daemon
            // that keeps running without its sensor looks alive on the feed.
            eprintln!("loa-motion: no GPIO: {e}");
            std::process::exit(1);
        }
    };
    if let Err(e) = chip.claim_input(gpio) {
        eprintln!("loa-motion: cannot claim GPIO{gpio}: {e}");
        std::process::exit(1);
    }

    let state = Arc::new(Mutex::new(Remembered::default()));
    let mut poller = Poller {
        gpio,
        cooldown,
        sense: Arc::clone(&sense),
        state: Arc::clone(&state),
        last: false,
        pending: 0,
        last_fire: 0.0,
        on_ts: None,
    };

    // The counter is per-boot: a rebooted body starts at zero.
    let _ = poller.publish(pb::Pir {
        count: Some(0),
        ..Default::default()
    });
    // The full pin state on our own start — the other half of the handshake:
    // whenever this process comes up, the cortex hears everything rather than a
    // change against a state it does not have.
    let _ = sense.event(
        "boot",
        &[
            ("svc", "motion"),
            ("gpio", &gpio.to_string()),
            ("cooldown", &format!("{cooldown}")),
        ],
    );
    if let Ok(level) = chip.read(gpio) {
        let high = level != 0;
        poller.remember(|s| {
            s.high = high;
            if high {
                s.on_ts = Some(now());
            }
        });
        let _ = poller.publish(pb::Pir {
            high: Some(high),
            on_ts: if high { Some(now()) } else { None },
            ..Default::default()
        });
    }

    let answer = {
        let st = Arc::clone(&state);
        move || pb::envelope::Body::Pir(st.lock().expect("state").full())
    };
    if let Err(e) = sense.answer_init(loa_sense::DEFAULT_FEED, answer) {
        eprintln!("loa-motion: no init listener (a restarted cortex stays blind): {e}");
    }

    eprintln!("loa-motion: GPIO{gpio}, cooldown {cooldown}s, uplink {}", sense.inbound);
    loop {
        poller.tick(&chip);
        pause(POLL_PERIOD);
    }
}
