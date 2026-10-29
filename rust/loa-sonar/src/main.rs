//! loa-sonar — the ultrasonic ranger, in Rust. TRIG 23 / ECHO 22.
//!
//! THE ONE DRIVER THAT GETS BETTER IN RUST. The Python version's docstring says
//! it needs microsecond timing, and it did that inside an interpreter: a
//! `perf_counter_ns()` call and a `req.get_value()` SYSCALL in the loop that
//! measures when the echo pulse started and ended. Here the pulse is timed
//! against a monotonic clock with no interpreter between the read and the
//! measurement, which is the whole reason this driver wanted gpiod over pinctrl
//! in the first place.
//!
//! The distance maths is unchanged: centimetres = microseconds / 58.
use std::time::{Duration, Instant};

use loa_gpio::{Chip, DEFAULT_CHIP};
use loa_sense::{now, pause, setting, setting_bool, Sense};
use loa_topic::pb;

const DEFAULT_TRIG: f64 = 23.0;
const DEFAULT_ECHO: f64 = 22.0;
const DEFAULT_PERIOD: f64 = 1.0;
/// 30ms of echo wait is a ~500cm ceiling: beyond that there is nothing to hear,
/// and a wait that long must not become the loop's period.
const ECHO_TIMEOUT: Duration = Duration::from_micros(30_000);
/// The HC-SR04 wants a 10us trigger pulse.
const TRIG_PULSE: Duration = Duration::from_micros(10);
/// The module answers in microseconds of echo; this is the divisor.
const US_PER_CM: f64 = 58.0;

/// One measurement, in centimetres. None = nothing heard (no module, out of
/// range, or a pulse too long to be a distance).
fn measure(chip: &Chip, trig: i32, echo: i32) -> Option<f64> {
    // Trigger: 10us high, then low.
    chip.write(trig, 1).ok()?;
    let t0 = Instant::now();
    while t0.elapsed() < TRIG_PULSE {
        std::hint::spin_loop();
    }
    chip.write(trig, 0).ok()?;

    // Wait for the echo to go high.
    let started = Instant::now();
    loop {
        if chip.read(echo).ok()? != 0 {
            break;
        }
        if started.elapsed() > ECHO_TIMEOUT {
            return None;
        }
    }
    // ...and then time how long it stays high.
    let high_at = Instant::now();
    loop {
        if chip.read(echo).ok()? == 0 {
            break;
        }
        if high_at.elapsed() > ECHO_TIMEOUT {
            return None;
        }
    }
    let us = high_at.elapsed().as_secs_f64() * 1e6;
    // A zero-width echo is not a distance, it is a glitch on the line.
    if us <= 0.0 {
        return None;
    }
    Some(us / US_PER_CM)
}

fn main() {
    let trig = setting("LOA_SENSE_TRIG", "sense", "trig", DEFAULT_TRIG) as i32;
    let echo = setting("LOA_SENSE_ECHO", "sense", "echo", DEFAULT_ECHO) as i32;
    let period = setting("LOA_SENSE_PERIOD", "sense", "period", DEFAULT_PERIOD);
    let enabled = setting_bool("LOA_SENSE_SNR_ENABLED", "sense", "snr_enabled", true);

    let sense = match Sense::open(loa_sense::DEFAULT_INBOUND) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("loa-sonar: no uplink to the cortex: {e}");
            std::process::exit(1);
        }
    };

    // The init answer: the last range and the count, so a restarted cortex is
    // not blind to a sensor that only speaks when it hears something.
    let last = std::sync::Arc::new(std::sync::Mutex::new((0u32, None::<f64>, None::<f64>)));
    {
        let st = std::sync::Arc::clone(&last);
        let answer = move || {
            let (count, cm, ts) = *st.lock().expect("sonar state");
            pb::envelope::Body::Sonar(pb::Sonar {
                cm,
                count: Some(count),
                ts,
            })
        };
        if let Err(e) = sense.answer_init(loa_sense::DEFAULT_FEED, answer) {
            eprintln!("loa-sonar: no init listener (a restarted cortex stays blind): {e}");
        }
    }

    let _ = sense.send(pb::envelope::Body::Sonar(pb::Sonar {
        count: Some(0),
        ..Default::default()
    }));

    if !enabled {
        // A disabled sonar is SILENT: no pings, no chirps, no reads. It stays
        // up rather than exiting, because the unit is Restart=always and
        // exiting here would spin a restart loop for a sensor that is
        // deliberately off.
        let _ = sense.event("boot", &[("svc", "sonar"), ("enabled", "false")]);
        eprintln!("loa-sonar: DISABLED by setting (snr_enabled) — staying up, not measuring");
        loop {
            pause(3600.0);
        }
    }

    let chip = match Chip::open(DEFAULT_CHIP) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("loa-sonar: no GPIO: {e}");
            std::process::exit(1);
        }
    };
    if let Err(e) = chip.claim_output(trig, 0) {
        eprintln!("loa-sonar: cannot claim TRIG GPIO{trig} as an output: {e}");
        std::process::exit(1);
    }
    if let Err(e) = chip.claim_input(echo) {
        eprintln!("loa-sonar: cannot claim ECHO GPIO{echo} as an input: {e}");
        std::process::exit(1);
    }

    let _ = sense.event(
        "boot",
        &[
            ("svc", "sonar"),
            ("trig", &trig.to_string()),
            ("echo", &echo.to_string()),
            ("period", &format!("{period}")),
        ],
    );
    eprintln!("loa-sonar: TRIG GPIO{trig} / ECHO GPIO{echo} every {period}s");

    loop {
        if let Some(cm) = measure(&chip, trig, echo) {
            let ts = now();
            let count = {
                let mut st = last.lock().expect("sonar state");
                st.0 += 1;
                st.1 = Some(cm);
                st.2 = Some(ts);
                st.0
            };
            let _ = sense.send(pb::envelope::Body::Sonar(pb::Sonar {
                cm: Some(cm),
                count: Some(count),
                ts: Some(ts),
            }));
        }
        pause(period);
    }
}
