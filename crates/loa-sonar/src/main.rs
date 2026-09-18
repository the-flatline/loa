//! loa-sonar — the ultrasonic ranger daemon. TRIG 23 / ECHO 22.
//!
//! The echo pulse is timed by the tightest thing the daemon can do: a spin
//! loop around lgGpioRead whose clock is `Instant::now()` — the spin IS the
//! timestamp source. The Python version did the same inside an interpreter,
//! with a syscall and a `perf_counter` call per loop pass; here there is no
//! interpreter between the read and the measurement. The distance maths is
//! unchanged: centimetres = microseconds / 58.
mod state;

use loa_sense::gpio::{spin_for, Chip, DEFAULT_CHIP};
use loa_sense::{now_s, secs_from_args, setting, setting_bool, Sense};
use loa_wire::pb;
use state::Snr;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

const DEFAULT_TRIG: f64 = 23.0;
const DEFAULT_ECHO: f64 = 22.0;
/// One ping per second — the reference's DEFAULT_SNR_PERIOD.
const DEFAULT_PERIOD: f64 = 1.0;
/// 30ms of echo wait is a ~500cm ceiling: beyond that there is nothing to
/// hear, and a wait that long must not become the loop's period.
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
    spin_for(TRIG_PULSE);
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
        std::hint::spin_loop();
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
        std::hint::spin_loop();
    }
    let us = high_at.elapsed().as_secs_f64() * 1e6;
    // A zero-width echo is not a distance, it is a glitch on the line.
    if us <= 0.0 {
        return None;
    }
    Some(us / US_PER_CM)
}

#[tokio::main]
async fn main() {
    let name = "loa-sonar";
    eprintln!("{name}: starting (uplink {}, feed {})",
        loa_sense::inbound_endpoint(), loa_sense::feed_endpoint());
    let secs = secs_from_args(name);
    let trig = setting("LOA_SENSE_TRIG", "sense", "trig", DEFAULT_TRIG) as i32;
    let echo = setting("LOA_SENSE_ECHO", "sense", "echo", DEFAULT_ECHO) as i32;
    let period = setting("LOA_SENSE_PERIOD", "sense", "period", DEFAULT_PERIOD);
    let enabled = setting_bool("LOA_SENSE_SNR_ENABLED", "sense", "snr_enabled", true);
    // The publish gate, on top of the sample clock: news when the RANGE MOVED
    // (or after the silence window). Default 0 = every measurement is news,
    // which is what the reference did — it published every cm at 1Hz. The
    // threshold exists so a still room can go quiet on the wire; the
    // doctrine's "publish when the number moves" is this, tuned on the bench.
    let move_cm = setting("LOA_SENSE_MOVE_CM", "sense", "snr_move_cm", 0.0);
    let quiet_s = setting("LOA_SENSE_QUIET_S", "sense", "snr_quiet_s", 30.0);

    let sense = match Sense::open() {
        Ok(s) => s,
        Err(e) => {
            eprintln!("{name}: cannot open the wire: {e}");
            std::process::exit(1);
        }
    };

    // The init answer: the whole current state, held where both loops can
    // reach it.
    let snr = Arc::new(Mutex::new(Snr::default()));
    {
        let st = Arc::clone(&snr);
        sense.on_full_payload(move || pb::envelope::Body::Sonar(st.lock().expect("snr state").full()));
    }

    // A fresh daemon, a cortex already up: the full payload once at start.
    {
        let st = snr.lock().expect("snr state");
        let _ = sense.publish(pb::envelope::Body::Sonar(st.full()));
    }

    let _ = sense.event(
        "boot",
        &[
            ("svc", "sonar"),
            ("trig", &trig.to_string()),
            ("echo", &echo.to_string()),
            ("period", &format!("{period}")),
            ("enabled", if enabled { "true" } else { "false" }),
        ],
    );

    if !enabled {
        // A disabled sonar is SILENT: no pings, no chirps, no reads. It stays
        // up rather than exiting, because the unit is Restart=always and
        // exiting here would spin a restart loop for a sensor that is
        // deliberately off.
        eprintln!("{name}: DISABLED by setting (snr_enabled) — staying up, not measuring");
        sense.run_loop(Duration::from_secs(3600), secs, |_| {}).await;
        sense.report(name, secs);
        return;
    }

    let chip = match Chip::open(DEFAULT_CHIP) {
        Ok(c) => c,
        Err(e) => {
            // Without the pin there is no sense to be. Say so and stop: a
            // daemon that keeps running without its sensor looks alive on
            // the feed.
            eprintln!("{name}: no GPIO: {e}");
            std::process::exit(1);
        }
    };
    if let Err(e) = chip.claim_output(trig, 0) {
        eprintln!("{name}: cannot claim TRIG GPIO{trig} as an output: {e}");
        std::process::exit(1);
    }
    if let Err(e) = chip.claim_input(echo) {
        eprintln!("{name}: cannot claim ECHO GPIO{echo} as an input: {e}");
        std::process::exit(1);
    }
    eprintln!("{name}: TRIG GPIO{trig} / ECHO GPIO{echo} every {period}s (move gate {move_cm}cm)");

    let snr_loop = Arc::clone(&snr);
    sense
        .run_loop(Duration::from_secs_f64(period.max(0.05)), secs, move |sense| {
            // The tick handler runs on the async runtime; a measurement is a
            // bounded spin (30ms ceiling), not an unbounded block, so this is
            // the whole sample path.
            let Some(cm) = measure(&chip, trig, echo) else { return };
            let ts = now_s();
            let msg = {
                let mut st = snr_loop.lock().expect("snr state");
                st.measured(cm, ts, move_cm, quiet_s)
            };
            if let Some(m) = msg {
                if let Err(e) = sense.publish(pb::envelope::Body::Sonar(m)) {
                    eprintln!("{name}: push: {e}");
                }
            }
        })
        .await;
    sense.report(name, secs);
}
