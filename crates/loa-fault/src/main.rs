//! loa-fault — the process: probes, the wire, the file. No decisions here;
//! every one lives in loa_fault::evaluate (the pure core, pinned by tests).
//!
//! The body's own pain sense, as a daemon. Running ON the loa is the point
//! (ref:1-5): the thing being checked reports on itself — no SSH, no network,
//! no dependency on the brain being reachable.
//!
//! The reference was a systemd oneshot + a 60 s timer + a 2.5 s init window
//! per run. This carries the clock itself (SWEEP_PERIOD_S, from the timer's
//! OnUnitActiveSec) and lives, which strictly improves the init answer: the
//! ask is now answered in any second of the minute, not only the 2.5 the
//! last run happened to linger. Every ask gets a FRESH sweep — never a
//! replay — because the answer must be current (ref:402-412).
//!
//! Modes (the reference read argv with one hand — `--quiet` was the whole
//! parser, ref:395 — so there is no argparse to carry):
//!   (default)  sweep on start, then every 60 s; answer every `init` ask
//!   --once     the reference's oneshot life: one sweep (file + push + print),
//!              exit 0 quiet / 1 hurting — for timers, cron, and the bench
//!   --quiet    print faults only (the reference's flag, verbatim)
//!   --report   print the cached /dev/shm report without sweeping — the
//!              CLI view of the file status() read (ref:366-384)
//!
//! SCRIPTS — the reference's `_check_scripts` (ref:124-139, the "SCRIPTS 0B"
//! row) is DROPPED, and this is why in the file: the four names it watched
//! (ref:44: loa-cortex, loa-weather, loa-fault, loa-topic-tail) were ALL
//! Python console scripts living in the venv's bin, and the venv dies with
//! this port. The reference's own comment said it (ref:39-42): the Rust
//! binaries in /usr/local/bin "cannot be zeroed by a venv write and this
//! check cannot see them — their unit checks cover them being missing.
//! Naming them here would be a check that can never fire." Once Python is
//! gone there is nothing LEFT for the check to fire on: the zero-byte
//! failure mode (203/EXEC, Restart=always, silent crash-loop — the exact
//! bug that born this module, ref:7-11) surfaces through the unit roster as
//! `{SHORT} LOOP` for every member, Rust included. A check that can never
//! fire is worse than no check. The roster is the assertion.

use std::collections::HashMap;
use std::process;
use std::sync::mpsc::{self, Sender as ChanSender};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use loa_wire::{decode, envelope, pb, Sender};

use loa_fault::{
    condition, evaluate, format_report, BusStatus, Inputs, Report, Row, Services, SvcState,
    FAULTS_PATH, SWEEP_PERIOD_S, UNITS,
};
use loa_common::condition as cond_words;

/// How long a probe may run before the sweep proceeds without it.
/// ref:65-72 (_run: shell, capture, timeout=8, never raise).
const PROBE_TIMEOUT_S: u64 = 8;
/// How long the FIRST sweep waits for a weather frame to land before it
/// sweeps without one. ref:310-322 listened 1.5 s at every run; on-Pi that
/// wait ran ~60 times a day — this pays it once, at boot, and keeps a live
/// cache after. A body with no feed goes sensor-STALE inside 10 min, which
/// is a row, which is the honest signal.
const FEED_WAIT_S: u64 = 2;

fn now_s() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

// ---------------------------------------------------------------------------
// Probes. Each mirrors its reference function one for one.
// ---------------------------------------------------------------------------

/// ref:65-72 — shell out, capture, trim, 8 s ceiling, never raise. A timeout
/// or a missing shell is rc=1 with the reason in stderr, exactly like the
/// reference's except-branch; the sweep keeps going either way.
fn run(cmd: &str) -> (i32, String, String) {
    run_timeout(cmd, Duration::from_secs(PROBE_TIMEOUT_S))
}

fn run_timeout(cmd: &str, limit: Duration) -> (i32, String, String) {
    let mut child = match process::Command::new("/bin/sh")
        .arg("-c")
        .arg(cmd)
        .stdout(process::Stdio::piped())
        .stderr(process::Stdio::piped())
        .spawn()
    {
        Ok(c) => c,
        Err(e) => return (1, String::new(), format!("OSError: {e}")),
    };
    let started = Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(_)) => {
                let out = child.wait_with_output();
                return match out {
                    Ok(o) => (
                        o.status.code().unwrap_or(-1),
                        String::from_utf8_lossy(&o.stdout).trim().to_string(),
                        String::from_utf8_lossy(&o.stderr).trim().to_string(),
                    ),
                    Err(_) => (-1, String::new(), String::new()),
                };
            }
            Ok(None) => {
                if started.elapsed() >= limit {
                    let _ = child.kill();
                    let _ = child.wait();
                    return (1, String::new(), "TimeoutExpired".into());
                }
                thread::sleep(Duration::from_millis(10));
            }
            Err(e) => {
                let _ = child.kill();
                let _ = child.wait();
                return (1, String::new(), format!("OSError: {e}"));
            }
        }
    }
}

/// ref:91-121 — `systemctl is-active {unit}.service`. Empty stdout reads as
/// "unknown" (the reference's `out or "unknown"`, ref:93) and unknown DOWNs
/// the unit. A dead check is never read as alive.
fn is_active(unit: &str) -> SvcState {
    let (_rc, out, _err) = run(&format!("systemctl is-active {unit}.service"));
    SvcState::parse(if out.is_empty() { "unknown" } else { &out })
}

/// ref:108 — the ring's inactive row asks whether the unit FILE exists, so
/// it can tell a stopped service from a missing one.
fn unit_file_exists(unit: &str) -> bool {
    std::path::Path::new(&format!("/etc/systemd/system/{unit}.service")).exists()
}

fn probe_services() -> Services {
    let mut out = HashMap::new();
    for unit in UNITS {
        let state = is_active(unit);
        // The file probe only feeds the ring-inactive text; asking it for
        // every unit costs one stat() and keeps the map's shape uniform.
        out.insert(unit.to_string(), (state, unit_file_exists(unit)));
    }
    out
}

/// ref:143-164 — the bus-holder probe is `sudo fuser -v {bus} 2>&1`; a
/// holder line is any output line naming the bus path (the "2>&1" carries
/// fuser's table over, since it prints it on stderr). Same command, same
/// filter, same merge.
fn probe_buses() -> BusStatus {
    let mut out = BusStatus::new();
    for (bus, _expected) in loa_fault::BUS_OWNERS {
        if !std::path::Path::new(bus).exists() {
            out.insert(bus.into(), (false, Vec::new())); // ref:145: no node, no claim
            continue;
        }
        let (_rc, merged, err) = run(&format!("sudo fuser -v {bus} 2>&1"));
        let table = if merged.is_empty() { err.as_str() } else { merged.as_str() };
        let holders = table.lines().filter(|ln| ln.contains(bus)).map(str::to_string).collect();
        out.insert(bus.into(), (true, holders));
    }
    out
}

/// ref:244-254 — root usage. shutil.disk_usage reads statvfs: bytes are
/// f_frsize * blocks, `free` is f_bavail (usable), and used_pct is
/// int(100 * (1 - free/total)). libc's statvfs is the direct door; a `df`
/// subprocess is the same number at one crash surface further out.
fn probe_disk() -> Option<(u64, f64, f64)> {
    let mut st: libc::statvfs = unsafe { std::mem::zeroed() };
    let rc = unsafe { libc::statvfs(b"/\0".as_ptr() as *const libc::c_char, &mut st) };
    if rc != 0 {
        return None; // ref:249: an OSError returns silently — no row
    }
    let frsize = st.f_frsize as f64;
    let total = (st.f_blocks as f64) * frsize;
    let free = (st.f_bavail as f64) * frsize;
    if total <= 0.0 {
        return None;
    }
    // int(): truncation, same as the reference's `int(100 * ...)`.
    let used_pct = (100.0 * (1.0 - free / total)) as u64;
    Some((used_pct, free / 1e9, total / 1e9))
}

/// ref:179-185 — `vcgencmd get_throttled` prints `throttled=0x...`; absent
/// binary, rc!=0, silent (a bench box is not a Pi fault).
fn probe_throttled() -> Option<u64> {
    let (rc, out, _) = run("vcgencmd get_throttled");
    if rc != 0 {
        return None;
    }
    let hex = out.split('=').nth(1)?.trim().trim_start_matches("0x").trim_start_matches("0X");
    u64::from_str_radix(hex, 16).ok()
}

/// ref:167-175 — `vcgencmd measure_temp` prints `temp=84.5'C`.
fn probe_temp() -> Option<f64> {
    let (rc, out, _) = run("vcgencmd measure_temp");
    if rc != 0 {
        return None;
    }
    let val = out.split('=').nth(1)?.trim().trim_end_matches("'C").trim().to_string();
    val.parse().ok()
}

/// ref:257-267 — "the body does not curl its own door": the unit's raw
/// is-active word plus ":<port>" anywhere in `ss -ltn` (the reference's
/// substring test, kept exactly — the port is env-driven because the cortex
/// binds LOA_API_PORT).
fn probe_api(port: &str) -> (String, bool) {
    let state = {
        let (_rc, out, _err) = run("systemctl is-active loa-cortex.service");
        if out.is_empty() { "unknown".to_string() } else { out }
    };
    let (_rc, out, _err) = run("ss -ltn");
    (state, out.contains(&format!(":{port}")))
}

/// ref:302-307 — the first 8 chars of the kernel boot id, or None.
fn probe_boot_id() -> Option<String> {
    std::fs::read_to_string("/proc/sys/kernel/random/boot_id")
        .ok()
        .map(|s| s.trim().chars().take(8).collect())
}

/// Where commands and sockets resolve from the env — one vocabulary drives
/// the body (loa-cortex/src/main.rs binds these; ripperdoc's feed_endpoint
/// is the same resolution read from the client side).
fn api_port() -> String {
    std::env::var("LOA_API_PORT")
        .ok()
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| loa_fault::API_PORT_DEFAULT.to_string())
}

fn inbound_endpoint() -> String {
    std::env::var("LOA_INBOUND")
        .ok()
        .filter(|s| !s.is_empty())
        .unwrap_or(format!("tcp://127.0.0.1:{}", loa_wire::INBOUND_PORT))
}

/// Where the feed lives. The cortex binds 0.0.0.0, which a client cannot
/// connect to, so the default names loopback; LOA_TOPIC_ENDPOINT overrides
/// — and otherwise the API_BIND host carries the port, exactly like the
/// cortex's own resolution (ripp...feed_endpoint, loa-cortex main.rs).
fn feed_endpoint() -> String {
    if let Ok(ep) = std::env::var("LOA_TOPIC_ENDPOINT") {
        if !ep.is_empty() {
            return ep;
        }
    }
    let host = std::env::var("LOA_API_BIND")
        .ok()
        .filter(|s| !s.is_empty() && s != "0.0.0.0" && s != "::")
        .unwrap_or_else(|| "127.0.0.1".into());
    format!("tcp://{host}:{}", loa_wire::FEED_PORT)
}

// ---------------------------------------------------------------------------
// The feed thread: weather in, init asks forwarded. Every frame is read;
// nothing is coalesced away.
// ---------------------------------------------------------------------------

/// The last weather reading off the feed (the reference's `_feed_state`
/// mirrored the whole feed; the only fields the sensor check reads are the
/// value+ts pairs, and Weather carries them — temp_c/temp_ts and
/// pressure_hpa/baro_ts, both stamped by the reading's ts).
#[derive(Default)]
struct SensorCache {
    weather: Option<pb::Weather>,
}

fn spawn_feed(asks: ChanSender<()>) -> Arc<Mutex<SensorCache>> {
    let cache = Arc::new(Mutex::new(SensorCache::default()));
    let shared = Arc::clone(&cache);
    let endpoint = feed_endpoint();
    thread::spawn(move || {
        let ctx = zmq::Context::new();
        let sock = match ctx.socket(zmq::SUB) {
            Ok(s) => s,
            Err(e) => {
                eprintln!("loa-fault: no SUB socket: {e}");
                return;
            }
        };
        let _ = sock.set_linger(0);
        // The full topic list, not "" — same filter posture as every other
        // consumer in loa-wire. init + weather are all this daemon reads.
        for ep in [&endpoint] {
            if let Err(e) = sock.connect(ep) {
                eprintln!("loa-fault: feed connect {ep}: {e}");
                return;
            }
        }
        for t in loa_wire::TOPICS {
            let _ = sock.set_subscribe(t.as_bytes());
        }
        let mut first_error = true;
        loop {
            // Read ALL queued frames, one by one. Subscriber::drain keeps
            // only the NEWEST message — right for a renderer, a lie here:
            // the cortex asks `init` six times, and a weather burst sharing
            // a drain window would swallow the asks, which is the exact
            // mute-for-300-s failure the reference warns about (ref:402-412).
            loop {
                match zmq::poll(&mut [sock.as_poll_item(zmq::POLLIN)], 100) {
                    Ok(0) => break,        // nothing more right now
                    Ok(_) => {}
                    Err(e) => {
                        eprintln!("loa-fault: feed poll: {e}");
                        return;
                    }
                }
                let parts = match (sock.recv_bytes(zmq::DONTWAIT), sock.recv_bytes(zmq::DONTWAIT)) {
                    (Ok(t), Ok(p)) => [t.to_vec(), p.to_vec()],
                    _ => break, // half a message is not a thing on this socket
                };
                let topic = String::from_utf8_lossy(&parts[0]).to_string();
                match topic.as_str() {
                    "init" => {
                        // Count the ask, not the decode: an init that
                        // fails the schema check still means the cortex
                        // wants a fresh report, and our answer will expose
                        // the mismatch on the inbound leg instead.
                        let _ = asks.send(());
                    }
                    "weather" => {
                        match decode(parts) {
                            Ok(msg) => {
                                if let Some(pb::envelope::Body::Weather(w)) = msg.env.body {
                                    *shared.lock().unwrap_or_else(|p| p.into_inner()) =
                                        SensorCache { weather: Some(w) };
                                }
                            }
                            Err(e) if first_error => {
                                // Say it once; a broken feed says it forever.
                                eprintln!("loa-fault: bad weather frame: {e}");
                                first_error = false;
                            }
                            Err(_) => {}
                        }
                    }
                    _ => {} // everything else is not this reader's subject
                }
            }
        }
    });
    cache
}

// ---------------------------------------------------------------------------
// The report: the /dev/shm cache and the wire.
// ---------------------------------------------------------------------------

/// The file's JSON (ref:292-299's report dict, written the way json.dump
/// would): {"ts": ..., "boot": ..., "rows": [{"level","code","text",
/// "face"?}], "faults": n, "warns": n}. `face` is only present when the
/// reference's dict carried one (ref:199-206 sets it for rails/sensor rows;
/// the json keeps the row dict's exact keys). Hand-rolled: no serde, and
/// the report's shape is this daemon's own contract.
fn report_to_json(rep: &Report) -> String {
    fn esc(s: &str) -> String {
        let mut out = String::with_capacity(s.len() + 8);
        for c in s.chars() {
            match c {
                '"' => out.push_str("\\\""),
                '\\' => out.push_str("\\\\"),
                '\n' => out.push_str("\\n"),
                '\r' => out.push_str("\\r"),
                '\t' => out.push_str("\\t"),
                c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
                c => out.push(c),
            }
        }
        out
    }
    let rows: Vec<String> = rep
        .rows
        .iter()
        .map(|r| {
            let face = if r.face.is_empty() {
                String::new()
            } else {
                format!(", \"face\": \"{}\"", esc(&r.face))
            };
            format!(
                "{{\"level\": \"{}\", \"code\": \"{}\", \"text\": \"{}\"{}}}",
                esc(&r.level), esc(&r.code), esc(&r.text), face
            )
        })
        .collect();
    let boot = match &rep.boot {
        Some(b) => format!("\"{}\"", esc(b)),
        None => "null".into(),
    };
    format!(
        "{{\"ts\": {}, \"boot\": {}, \"rows\": [{}], \"faults\": {}, \"warns\": {}}}",
        rep.ts,
        boot,
        rows.join(", "),
        rep.faults,
        rep.warns,
    )
}

/// ref:344-349 — write tmp then os.replace: a reader never sees half a
/// report. A failed write is passed over (ref:349 `except OSError: pass`) —
/// the /dev/shm cache is a convenience; the wire carries the truth.
fn write_report(rep: &Report) {
    let tmp = format!("{FAULTS_PATH}.tmp");
    if std::fs::write(&tmp, report_to_json(rep)).is_ok() {
        let _ = std::fs::rename(&tmp, FAULTS_PATH);
    }
}

/// ref:336-363 — sweep, cache in RAM, send UP on the fault topic. The file
/// is a local cache for the CLI and dies with the machine; the topic is how
/// the cortex learns what hurts (ref:338-341). The cortex REBUILDS its own
/// fault view from these rows plus its injected DB DOWN and republishes on
/// the consumer topic (loa-cortex/src/main.rs on_inbox + build_fault) — the
/// sweep pushes the built pb::Fault as-is; there is no relay route for it
/// and none wanted.
fn publish(rep: &Report, push: Option<&Sender>) {
    write_report(rep);
    let Some(push) = push else { return };
    let fault = pb::Fault {
        rows: rep
            .rows
            .iter()
            .map(|r| pb::fault::Row {
                level: r.level.clone(),
                code: r.code.clone(),
                text: r.text.clone(),
                // optional string: the reference ALWAYS sets face (ref:359
                // `face=str(r.get("face") or "")`), so presence rides even when empty.
                face: Some(r.face.clone()),
            })
            .collect(),
        condition: Some(rep.condition.clone()),
        ts: Some(rep.ts),
    };
    if let Err(e) = push.push("fault", &envelope(pb::envelope::Body::Fault(fault))) {
        // A publish failure is not itself a fault (ref:361) — the sweep is
        // the fault reporter, and self-reporting a lost push would loop the
        // story. It is loud on stderr, and a cortex that never gets a
        // report goes mute on the consumer side, which IS the row.
        eprintln!("loa-fault: push fault: {e}");
    }
}

// ---------------------------------------------------------------------------
// One sweep, end to end. Thin over evaluate.
// ---------------------------------------------------------------------------

fn sweep(cache: &Arc<Mutex<SensorCache>>, push: Option<&Sender>, now: f64) -> Report {
    let services = probe_services();
    let bus = probe_buses();
    let (i2c_door, device_tree) = (
        std::path::Path::new("/dev/i2c-1").exists(), // ref:273
        std::path::Path::new("/proc/device-tree").exists(),
    );
    let port = api_port();
    let (cortex_state, api_listening) = probe_api(&port);

    let sensors = {
        let mut m = HashMap::new();
        let w = cache
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .weather
            .clone();
        if let Some(w) = w {
            // The reference read temp_ts/baro_ts off the mirror (ref:53's
            // SENSORS ts keys); the Weather message stamps one ts and the
            // cortex merge copies it to both keys (loa-cortex/src/state.rs).
            let ts = w.ts;
            if let Some(t) = w.temp_c {
                m.insert("temp_c".to_string(), (Some(t), ts));
            } else {
                m.insert("temp_c".to_string(), (None, None));
            }
            if let Some(p) = w.pressure_hpa {
                m.insert("pressure_hpa".to_string(), (Some(p), ts));
            } else {
                m.insert("pressure_hpa".to_string(), (None, None));
            }
        }
        m
    };

    let inputs = Inputs {
        services: &services,
        bus: &bus,
        disk: probe_disk(),
        sensors: &sensors,
        throttled: probe_throttled(),
        temp_val: probe_temp(),
        cortex_state,
        api_listening,
        api_port: port,
        i2c_door,
        device_tree,
        now,
    };
    let (rows, cond) = evaluate(&inputs);
    let mut rep = Report { ts: now, boot: probe_boot_id(), rows, faults: 0, warns: 0, condition: cond };
    rep.faults = rep.rows.iter().filter(|r| r.level == loa_fault::FAULT).count();
    rep.warns = rep.rows.iter().filter(|r| r.level == loa_fault::WARN).count();
    publish(&rep, push);
    rep
}

/// The reference's clock string (ref:378: %Y-%m-%d %H:%M %Z off localtime).
fn clock_string(ts: f64) -> String {
    let t = ts as libc::time_t;
    let mut tm: libc::tm = unsafe { std::mem::zeroed() };
    if unsafe { libc::localtime_r(&t, &mut tm) }.is_null() {
        return format!("{ts:.0}");
    }
    let zone = unsafe {
        if tm.tm_zone.is_null() {
            String::new()
        } else {
            std::ffi::CStr::from_ptr(tm.tm_zone).to_string_lossy().into_owned()
        }
    };
    format!(
        "{:04}-{:02}-{:02} {:02}:{:02} {zone}",
        tm.tm_year + 1900,
        tm.tm_mon + 1,
        tm.tm_mday,
        tm.tm_hour,
        tm.tm_min,
    )
}

// ---------------------------------------------------------------------------
// --report: the cached view (ref:366-384's status + format_report pair).
// ---------------------------------------------------------------------------

/// Parse what the sweep wrote: the scalars and the rows, by hand — the file
/// is this daemon's own shape (see report_to_json) and a full JSON tree for
/// four keys would be a dependency. Anything unparseable reads as "never
/// swept", which is the honest status for a cache that cannot be trusted.
fn parse_report(text: &str) -> Option<Report> {
    let start = text.find("{\"ts\":")?;
    let body = &text[start..];
    let ts = {
        let at = body.find("\"ts\": ")? + 6;
        let end = body[at..]
            .find(|c: char| c == ',' || c == '}' || c == ']')
            .unwrap_or(body.len() - at);
        body[at..at + end].trim().parse::<f64>().ok()?
    };
    let boot = {
        let at = body.find("\"boot\": ")? + 8;
        let rest = &body[at..];
        if rest.starts_with("null") {
            None
        } else {
            let s = rest.strip_prefix('"')?;
            let end = s.find('"')?;
            Some(s[..end].to_string())
        }
    };
    let scalar = |key: &str| -> Option<usize> {
        let at = body.find(key)? + key.len();
        let end = body[at..]
            .find(|c: char| c == ',' || c == '}' || c == ']')
            .unwrap_or(body.len() - at);
        body[at..at + end].trim().parse().ok()
    };
    let faults = scalar("\"faults\": ")?;
    let warns = scalar("\"warns\": ")?;
    let rows_at = body.find("\"rows\": [")? + 9;
    let rows_end = body[rows_at..].find(']')? + rows_at;
    let mut rows = Vec::new();
    let mut cur = &body[rows_at..rows_end];
    while let Some(o) = cur.find('{') {
        let Some(c) = cur[o..].find('}') else { break };
        let obj = &cur[o..o + c + 1];
        let field = |key: &str| -> String {
            obj.find(key)
                .map(|at| {
                    let s = &obj[at + key.len() + 1..];
                    let e = s.find('"').unwrap_or(0);
                    s[..e].to_string()
                })
                .unwrap_or_default()
        };
        rows.push(Row {
            level: field("\"level\": "),
            code: field("\"code\": "),
            text: field("\"text\": "),
            face: field("\"face\": "),
        });
        cur = &cur[o + c + 1..];
    }
    Some(Report {
        ts,
        boot,
        condition: String::new(), // not in the file; the viewer computes it
        faults,
        warns,
        rows,
    })
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------

fn main() {
    let argv: Vec<String> = std::env::args().collect();
    let quiet = argv.iter().any(|a| a == "--quiet");

    // The file-first life: print the cache, sweep nothing, open no socket.
    if argv.iter().any(|a| a == "--report") {
        let text = match std::fs::read_to_string(FAULTS_PATH) {
            Ok(t) => t,
            Err(_) => {
                // ref:366-372: status() returns {} when never swept, and
                // silence is not health.
                println!("{} — no sweep on file (never swept or cache cleared)", cond_words::MUTE);
                process::exit(1);
            }
        };
        let Some(rep) = parse_report(&text) else {
            println!("unreadable sweep cache at {FAULTS_PATH} — treating it as {}", cond_words::MUTE);
            process::exit(1);
        };
        let c = condition(Some(&rep), now_s()); // mute gate on the file's ts
        let cond_line = format!("condition {c}");
        let body = format_report(&rep, &clock_string(rep.ts), quiet);
        println!("{body}\n{cond_line}");
        process::exit(if rep.faults > 0 { 1 } else { 0 });
    }

    let push = match Sender::connect(&zmq::Context::new(), &inbound_endpoint()) {
        Ok(s) => Some(s),
        Err(e) => {
            // No PUSH leg is no wire: the inbound socket refuses to connect
            // only on a malformed endpoint, which is a config lie — say it
            // and exit; a daemon that starts broken reports a false well.
            eprintln!("loa-fault: no inbound leg on {}: {e}", inbound_endpoint());
            process::exit(2);
        }
    };

    let (ask_tx, ask_rx) = mpsc::channel::<()>();
    let cache = spawn_feed(ask_tx);

    if argv.iter().any(|a| a == "--once") {
        // The reference's oneshot life, grace included (ref:310 waited 1.5 s
        // for the feed at every run): pay it, then sweep exactly once.
        let deadline = Instant::now() + Duration::from_secs(FEED_WAIT_S);
        while Instant::now() < deadline {
            if cache.lock().unwrap_or_else(|p| p.into_inner()).weather.is_some() {
                break;
            }
            thread::sleep(Duration::from_millis(50));
        }
        let rep = sweep(&cache, push.as_ref(), now_s());
        if rep.faults > 0 || !quiet {
            println!("{}", format_report(&rep, &clock_string(rep.ts), quiet));
        }
        process::exit(if rep.faults > 0 { 1 } else { 0 }); // 0 quiet, 1 hurting
    }

    // The daemon's life: sweep on start, then every 60 s; answer EVERY ask.
    let rep = sweep(&cache, push.as_ref(), now_s());
    eprintln!(
        "loa-fault: first sweep — {} fault(s), {} warn(s), {}",
        rep.faults, rep.warns, rep.condition
    );
    let mut next_sweep = Instant::now() + Duration::from_secs(SWEEP_PERIOD_S);
    let mut asks_answered = 0u64;
    loop {
        // Wake on an ask or the period, whichever is sooner. recv_timeout
        // can't take a deadline, so poll the channel on a short tick and
        // recheck the clock — a 100 ms resolution on a 60 s cadence.
        match ask_rx.recv_timeout(Duration::from_millis(100)) {
            Ok(()) => {
                // A cortex whose RAM is empty just said "tell me again."
                // Re-sweep and re-push the FULL report, every time
                // (ref:402-412 — answering only the first ask is how a late
                // restart sits mute for a whole sweep period).
                let rep = sweep(&cache, push.as_ref(), now_s());
                asks_answered += 1;
                eprintln!(
                    "loa-fault: init #{asks_answered} — reswept, pushed ({} fault(s), {} warn(s), {})",
                    rep.faults, rep.warns, rep.condition
                );
                // The answer refreshed the truth; the clock starts again.
                next_sweep = Instant::now() + Duration::from_secs(SWEEP_PERIOD_S);
            }
            Err(mpsc::RecvTimeoutError::Timeout) => {
                if Instant::now() >= next_sweep {
                    let rep = sweep(&cache, push.as_ref(), now_s());
                    if rep.faults > 0 && !quiet {
                        // Periodic life logs what hurts; --once/--report are
                        // the print paths. Quiet keeps the journal clean.
                        eprintln!(
                            "loa-fault: {} fault(s), {} warn(s), {}",
                            rep.faults, rep.warns, rep.condition
                        );
                    }
                    next_sweep += Duration::from_secs(SWEEP_PERIOD_S);
                    let now = Instant::now();
                    if next_sweep <= now {
                        next_sweep = now + Duration::from_secs(SWEEP_PERIOD_S);
                    }
                }
            }
            Err(mpsc::RecvTimeoutError::Disconnected) => break, // the feed died
        }
    }
    eprintln!("loa-fault: feed closed; exiting hurting-by-silence");
    process::exit(1);
}
