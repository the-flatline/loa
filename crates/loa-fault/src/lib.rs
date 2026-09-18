//! loa-fault — the decision core. Every number and word in this file is
//! carried from `loa/fault/__main__.py`, with its reference line.
//!
//! Two precedences live here, deliberately different:
//!
//!   `sweep_condition`  what a FRESH sweep claims about its own rows:
//!                      hurts > niggle > well (ref __main__.py:327-333).
//!                      A new sweep just spoke — it is never mute.
//!   `condition`        how the body carries itself given a REPORT: mute >
//!                      hurts > niggle > well (ref __main__.py:58-77, the
//!                      consumer-side rule). mute OUTRANKS hurts: silence is
//!                      not health, and a report older than FAULTS_STALE_S —
//!                      the sense gone deaf — must never read as calm.
//!
//! The cortex does a THIRD thing: it rebuilds the fault view from the merged
//! rows plus its own injected DB DOWN (loa-cortex/src/build.rs:55). That is
//! the cortex's opinion of the feed; this crate must not duplicate it — the
//! sweep pushes rows and its own condition, and the cortex merges both.
//!
//! `evaluate` is pure: given the probe inputs it returns rows and the sweep's
//! condition. main.rs owns every I/O and is a thin shell over it.

use std::collections::HashMap;

use loa_common::condition;

pub const FAULT: &str = "fault";
pub const WARN: &str = "warn";

/// The roster (ref:29-33: six; loa-fault itself is the seventh — it is a unit
/// on the body, and a sweep whose own timer died must say so rather than keep
/// reporting well from a dead man's file. The reference's UNITS predates the
/// Rust rename; this is the seven-daemon roster the rebuild settled on).
/// loa-fault in the list is safe from self-doubt: the sweep process answers
/// for itself by running at all, and the unit it names is the one that
/// launched it. When the TIMER stops, nothing sweeps, the report ages past
/// FAULTS_STALE_S, and every consumer reads mute — a stopped timer therefore
/// needs no row to be seen. The row exists for the rarer case: the timer
/// alive, the unit deleted or failed at boot.
pub const UNITS: [&str; 7] = [
    "loa-cortex", "loa-oled", "loa-ring", "loa-motion", "loa-sonar", "loa-weather", "loa-fault",
];

/// Who should hold which SPI bus (ref:36-37). Two writers on one bus is the
/// classic ghost-in-the-panel fault.
pub const BUS_OWNERS: [(&str, &str); 2] =
    [("/dev/spidev0.0", "loa-oled"), ("/dev/spidev1.0", "loa-ring")];
pub const BUS_LABEL: [(&str, &str); 2] = [("/dev/spidev0.0", "FACE"), ("/dev/spidev1.0", "RING")];

pub const FAULTS_PATH: &str = "/dev/shm/loa-faults.json"; // ref:20 — RAM; the
/// body's live state dies with the machine and republishes on boot (ref:19).
pub const DISK_WARN_PCT: u64 = 85; // ref:51
/// One warn row at 85%, and that is the whole check — the reference has no
/// 90% fault tier (ref:251: a single `>= DISK_WARN_PCT` append). The port
/// carries the single tier; a second threshold would be an invention.
/// ref:52 — 10 min without a read: stop believing the number.
pub const SENSOR_STALE_S: f64 = 600.0;
/// ref:54 — 5 missed sweeps at 1/min = the sense has gone deaf.
pub const FAULTS_STALE_S: f64 = 300.0;
/// ref:53 — the two continuously-sampled sensors, as (face label, value key,
/// ts key). The PIR is NOT here: "no motion for three hours" is a quiet room,
/// not a fault (ref:218).
pub const SENSORS: [(&str, &str, &str); 2] =
    [("TEMP", "temp_c", "temp_ts"), ("BARO", "pressure_hpa", "baro_ts")];

/// The door. ref:261 checks the unit and :8765. The cortex's actual bind is
/// LOA_API_PORT (loa-cortex/src/main.rs, default 8765), so the number is an
/// input (Inputs::api_port) and the default is this.
pub const API_PORT_DEFAULT: &str = "8765";

/// The reference's cadence lives in deploy/loa-fault.timer
/// (OnUnitActiveSec=60, AccuracySec=5); the Rust daemon carries the clock.
pub const SWEEP_PERIOD_S: u64 = 60;

/// Every code the sweep emits, as a template: the variable word is the unit
/// short name (after "loa-"), sensor label, or bus label — the reference
/// builds them by format string (ref:102,120,152,162,193,201,231,237,252,265,
/// 274). A test asserts no check produces a code outside this list.
pub const CODES: [&str; 13] = [
    "{SHORT} DOWN",  // systemctl says nothing kind (ref:120)
    "{SHORT} LOOP",  // crash-looping under Restart=always (ref:102)
    "RING STOPPED",  // loa-ring inactive — honest, not "OFF" (ref:113)
    "BUS CLASH",     // a bus held by the wrong hand (ref:152)
    "{LABEL} DARK",  // nothing driving the bus (ref:162)
    "UNDERVOLT",     // 5V sagging right now (ref:193)
    "THROTTLED",     // capped to cope, not hot (ref:201)
    "HOT",           // capped to cope at >= 80C (ref:201)
    "{LABEL} NO TS", // a value with no timestamp — nothing can age it (ref:231)
    "{LABEL} STALE", // a value older than SENSOR_STALE_S (ref:237)
    "DISK {pct}%",   // root above DISK_WARN_PCT (ref:252)
    "API DEAD",      // cortex up but the door is not listening (ref:265)
    "I2C NO DOOR",   // /dev/i2c-1 absent: i2c-dev not loaded (ref:274)
];
// Ref:136 "SCRIPTS 0B" is NOT in the list — the check is dropped with the
// venv it watched (the why is in main.rs's header). Ref:287 "CHECK ERR" is
// not either: it wrapped a check that threw. In Rust a failed probe reads
// UNKNOWN/DOWN (the reference's own `out or "unknown"` path, ref:93), which
// is the honest row; a check that panics takes the sweep down loudly instead
// of dressing a bug up as a warn row.

/// One fault row, structured (ref:14-16: a short code for the face — it has
/// 128 pixels — and a longer line for the API and the log). `face` is "" when
/// the row has nothing short to show; the wire carries the empty string
/// (ref:359: `face=str(r.get("face") or "")`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Row {
    pub level: String, // FAULT | WARN
    pub code: String,
    pub text: String,
    pub face: String,
}

impl Row {
    pub fn new(level: &str, code: String, text: String) -> Self {
        Row { level: level.into(), code, text, face: String::new() }
    }
    pub fn with_face(level: &str, code: String, text: String, face: String) -> Self {
        Row { level: level.into(), code, text, face }
    }
}

/// What `systemctl is-active` said about a unit (ref:92-93: the parsed stdout;
/// empty output is "unknown").
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SvcState {
    Active,
    Inactive,
    Failed,
    /// activating | auto-restart | reloading — running and failing. NOT the
    /// same fault as dead: reading it as "device broken" is exactly the
    /// mistake this module exists to stop (ref:96-98).
    Looping,
    Unknown,
}

impl SvcState {
    /// The reference's exact parse (ref:94-99): the word systemctl printed;
    /// anything unrecognised is Unknown.
    pub fn parse(s: &str) -> Self {
        match s {
            "active" => SvcState::Active,
            "inactive" => SvcState::Inactive,
            "failed" => SvcState::Failed,
            "activating" | "auto-restart" | "reloading" => SvcState::Looping,
            _ => SvcState::Unknown,
        }
    }
    /// The word that goes into the row's text. A Looping row quotes
    /// whichever of its three words was seen; the caller carries the raw
    /// word through Inputs where the text needs it (the reference prints
    /// `({state})` verbatim, ref:104). `word()` is the fallback shape.
    pub fn word(&self) -> &'static str {
        match self {
            SvcState::Active => "active",
            SvcState::Inactive => "inactive",
            SvcState::Failed => "failed",
            SvcState::Looping => "activating",
            SvcState::Unknown => "unknown",
        }
    }
}

/// Per unit: its systemctl state, and whether the unit FILE exists (the
/// reference consults it only for the loa-ring inactive row, ref:108).
pub type Services = HashMap<String, (SvcState, bool)>;

/// Per SPI bus: whether the /dev node exists, and the `fuser -v` lines naming
/// it (ref:148 — a holder line is any fuser output line containing the path).
pub type BusStatus = HashMap<String, (bool, Vec<String>)>;

/// Per sensor value key: the last reading seen on the feed and its ts.
/// (None, None) means the sensor never spoke (ref:226-241 reads
/// temp_c/temp_ts and pressure_hpa/baro_ts off the mirrored feed state).
pub type SensorReadings = HashMap<String, (Option<f64>, Option<f64>)>;

/// The pure probe inputs for one sweep. Every field is something main.rs read
/// off the machine; nothing here knows how.
pub struct Inputs<'a> {
    pub services: &'a Services,
    pub bus: &'a BusStatus,
    /// (used_pct as the reference rounds it — int(100*(1-free/total)) — free
    /// GB, total GB). None when statvfs failed (ref:249 returns silently).
    pub disk: Option<(u64, f64, f64)>,
    pub sensors: &'a SensorReadings,
    /// vcgencmd get_throttled bits (ref:179-185). None off-Pi — silent.
    pub throttled: Option<u64>,
    pub temp_val: Option<f64>, // vcgencmd measure_temp (ref:167-175)
    /// What `systemctl is-active loa-cortex.service` printed, raw: the API
    /// check re-reads the unit and quotes whatever word it got (ref:262,266).
    pub cortex_state: String,
    pub api_listening: bool, // the door's port appears in `ss -ltn` (ref:263)
    pub api_port: String,    // the number quoted in the row (default: the const)
    pub i2c_door: bool,      // /dev/i2c-1 exists (ref:273)
    pub device_tree: bool,   // /proc/device-tree exists (ref:273)
    pub now: f64,
}

/// The face's word for a unit: the part after "loa-", upper-cased (ref:102,
/// ref:120 — `unit.split('-')[1].upper()`).
pub fn short(unit: &str) -> String {
    unit.split('-').nth(1).unwrap_or(unit).to_uppercase()
}

/// THE decision. Pure: inputs in, rows and the sweep's condition out. Order is
/// faults first, worst first, stable (ref:290-291's sort by level alone).
pub fn evaluate(i: &Inputs) -> (Vec<Row>, String) {
    let mut rows = Vec::new();

    // ---- units (ref:91-121) ----
    for unit in UNITS {
        let (state, unit_file_exists) = i
            .services
            .get(unit)
            .cloned()
            .unwrap_or((SvcState::Unknown, false));
        if state == SvcState::Active {
            continue;
        }
        if state == SvcState::Looping {
            rows.push(Row::new(
                FAULT,
                format!("{} LOOP", short(unit)),
                format!(
                    "{unit} is CRASH-LOOPING ({}) — running and failing. Check its \
                     log before believing anything downstream is broken.",
                    state.word()
                ),
            ));
        } else if state == SvcState::Inactive && unit == "loa-ring" {
            // "OFF" would overclaim: this daemon is what DRIVES the ring, and
            // losing it says nothing about the hardware (ref:109-110).
            rows.push(Row::new(
                WARN,
                "RING STOPPED".to_string(),
                format!(
                    "loa-ring not running — unit file {}",
                    if unit_file_exists { "present but stopped" } else { "MISSING from /etc/systemd/system" }
                ),
            ));
        } else {
            rows.push(Row::new(
                FAULT,
                format!("{} DOWN", short(unit)),
                format!("{unit} is {}", state.word()),
            ));
        }
    }

    // ---- buses (ref:143-164) ----
    for (bus, expected) in BUS_OWNERS {
        let (node_exists, holders) = i.bus.get(bus).cloned().unwrap_or((false, Vec::new()));
        if !node_exists {
            continue; // the hardware isn't there; nothing to blame (ref:145)
        }
        if holders.iter().any(|h| h.contains(expected)) {
            continue; // the right hand is on the bus
        }
        if !holders.is_empty() {
            rows.push(Row::new(
                FAULT,
                "BUS CLASH".to_string(),
                format!(
                    "{bus} held by something else: {}",
                    holders.iter().map(|h| h.trim()).collect::<Vec<_>>().join(" | ")
                ),
            ));
        } else {
            // spidev is write-only — no readback on a WS2812 chain or an
            // SH1106 panel — so name the OBSERVABLE fact: the hardware will
            // be dark (ref:156-159).
            let fallback = bus[bus.len() - 3..].to_uppercase();
            let label: &str = BUS_LABEL
                .iter()
                .find(|(b, _)| *b == bus)
                .map(|(_, l)| *l)
                .unwrap_or(&fallback);
            rows.push(Row::new(
                FAULT,
                format!("{label} DARK"),
                format!(
                    "nothing is driving {bus} — expected {expected}, so the {} is dark",
                    label.to_lowercase()
                ),
            ));
        }
    }

    // ---- rails (ref:178-207) ----
    if let Some(bits) = i.throttled {
        // One row PER CAUSE, never one per bit (ref:186-190): bits 1/2 are
        // the SoC's RESPONSE and flicker sample to sample; naming the cause
        // stops the page looking like it keeps changing its mind.
        let thr = bits & 0xE;
        if bits & 0x1 != 0 {
            rows.push(Row::new(
                FAULT,
                "UNDERVOLT".to_string(),
                format!(
                    "5V input sagging RIGHT NOW{} (throttled={:#x})",
                    if thr != 0 { " — SoC throttled to cope" } else { "" },
                    bits
                ),
            ));
        } else if thr != 0 {
            let hot = i.temp_val.map(|t| t >= 80.0).unwrap_or(true); // ref:199: unreadable temp counts as hot
            let face = match (hot, i.temp_val) {
                (true, Some(t)) => format!("HOT {t:.0}C"),
                (true, None) => "HOT".to_string(),
                (false, _) => "THROTTLED".to_string(),
            };
            let cause = format!(
                "{} (throttled={:#x})",
                match i.temp_val {
                    Some(t) => format!("SoC at {t}C — freq-capped/throttling to cope"),
                    None => "SoC throttling — temperature unreadable".to_string(),
                },
                bits
            );
            rows.push(Row::with_face(
                FAULT,
                (if hot { "HOT" } else { "THROTTLED" }).to_string(),
                cause,
                face,
            ));
        }
    }

    // ---- sensors (ref:210-241) ----
    for (label, val_key, _ts_key) in SENSORS {
        let (value, ts) = i.sensors.get(val_key).copied().unwrap_or((None, None));
        if value.is_none() {
            continue; // absent entirely: the face shows that (ref:228)
        }
        match ts {
            None => rows.push(Row::new(
                WARN,
                format!("{label} NO TS"),
                format!("{val_key} has a value but no timestamp — nothing can say how old it is"),
            )),
            Some(ts) => {
                let age = i.now - ts;
                if age > SENSOR_STALE_S {
                    rows.push(Row::with_face(
                        WARN,
                        format!("{label} STALE"),
                        format!(
                            "{val_key} last read {:.0} min ago — that number is a ghost, not a reading",
                            age / 60.0
                        ),
                        format!("{label} {}M OLD", (age / 60.0) as i64), // int() truncates
                    ));
                }
            }
        }
    }

    // ---- disk (ref:244-254) ----
    if let Some((used_pct, free_gb, _total)) = i.disk {
        if used_pct >= DISK_WARN_PCT {
            rows.push(Row::new(
                WARN,
                format!("DISK {used_pct}%"),
                format!("root filesystem {used_pct}% used ({free_gb:.1} GB free)"),
            ));
        }
    }

    // ---- api (ref:257-267) ----
    // "The body does not curl its own door" (ref:258-261): unit active AND
    // the port listening. A cortex up but unbound still reads as dead here.
    if i.cortex_state != "active" || !i.api_listening {
        rows.push(Row::new(
            FAULT,
            "API DEAD".to_string(),
            format!(
                "cortex not answering on :{} (unit '{}', port {})",
                if i.api_port.is_empty() { API_PORT_DEFAULT } else { i.api_port.as_str() },
                i.cortex_state,
                if i.api_listening { "listening" } else { "closed" }
            ),
        ));
    }

    // ---- i2c (ref:270-276) ----
    if !i.i2c_door && i.device_tree {
        rows.push(Row::new(
            WARN,
            "I2C NO DOOR".to_string(),
            "/dev/i2c-1 absent — i2c-dev not loaded (a blind bus, not a dead chip)".to_string(),
        ));
    }

    // Faults first, worst first, stable (ref:290-291: a sort key on level
    // alone — Vec order within a level is untouched).
    let order = |l: &str| if l == FAULT { 0 } else { 1 };
    rows.sort_by_key(|r| order(&r.level));

    let cond = sweep_condition(&rows);
    (rows, cond)
}

/// What a fresh sweep claims about its own rows (ref:327-333). This is the
/// condition INSIDE the published Fault message — the cortex merges it into
/// state verbatim (loa-cortex/src/state.rs merge) and its own rebuild may
/// differ once the DB row is injected. A new sweep is never mute: it just
/// spoke; the muteness of a stale report is `condition`'s job below.
pub fn sweep_condition(rows: &[Row]) -> String {
    if rows.iter().any(|r| r.level == FAULT) {
        condition::HURTS.to_string()
    } else if rows.iter().any(|r| r.level == WARN) {
        condition::NIGGLE.to_string()
    } else {
        condition::WELL.to_string()
    }
}

/// The sweep's report — the shape /dev/shm/loa-faults.json carries
/// (ref:292-299: {"ts","boot","rows":[...],"faults","warns"}). `condition`
/// rides for the file's readers; the reference's file has no condition key,
/// and the JSON writer keeps that exact — it is derived on read, via
/// `condition()` below, the same way the reference's CLI derives it.
#[derive(Debug, Clone)]
pub struct Report {
    pub ts: f64,
    pub boot: Option<String>, // None when /proc/sys/kernel/random/boot_id is unreadable (ref:302-307)
    pub rows: Vec<Row>,
    pub faults: usize,
    pub warns: usize,
    /// The sweep's own verdict (sweep_condition at build time). It rides the
    /// wire; it is NOT in the file — the reference's file has no condition
    /// key, and readers derive it from faults/warns via `condition` below.
    pub condition: String,
}

impl Report {
    pub fn new(ts: f64, boot: Option<String>, rows: Vec<Row>) -> Self {
        let condition = sweep_condition(&rows);
        Report {
            ts,
            boot,
            faults: rows.iter().filter(|r| r.level == FAULT).count(),
            warns: rows.iter().filter(|r| r.level == WARN).count(),
            condition,
            rows,
        }
    }
}

/// How the body should carry itself, given a report (ref:58-77). `None` is
/// "never swept" — mute. A report older than FAULTS_STALE_S is mute even when
/// its rows say hurts: the sweep that wrote it is dead, and its last words
/// outrank nothing. mute > hurts > niggle > well, exactly.
pub fn condition(report: Option<&Report>, now: f64) -> String {
    let Some(rep) = report else { return condition::MUTE.to_string() };
    if now - rep.ts > FAULTS_STALE_S {
        return condition::MUTE.to_string();
    }
    if rep.faults > 0 {
        return condition::HURTS.to_string();
    }
    if rep.warns > 0 {
        return condition::NIGGLE.to_string();
    }
    condition::WELL.to_string()
}

/// The CLI's report (ref:375-384). `time_str` arrives formatted by the
/// caller (%Y-%m-%d %H:%M %Z off localtime — formatting is I/O, not logic).
pub fn format_report(rep: &Report, time_str: &str, quiet: bool) -> String {
    let verb =
        if rep.faults > 0 { "HURTS" } else if rep.warns > 0 { "niggles" } else { "quiet" };
    let boot = rep.boot.clone().unwrap_or_default();
    let mut lines = vec![format!("{verb} — {time_str}  boot {boot}")];
    for r in &rep.rows {
        if quiet && r.level != FAULT {
            continue;
        }
        lines.push(format!("  {:<5} {:<12} {}", r.level, r.code, r.text));
    }
    lines.join("\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn all_active() -> Services {
        UNITS
            .iter()
            .map(|u| (u.to_string(), (SvcState::Active, true)))
            .collect()
    }

    fn inputs<'a>(
        services: &'a Services,
        bus: &'a BusStatus,
        sensors: &'a SensorReadings,
        now: f64,
    ) -> Inputs<'a> {
        Inputs {
            services,
            bus,
            disk: None, // no disk row unless a test sets one
            sensors,
            throttled: None,
            temp_val: None,
            cortex_state: "active".into(),
            api_listening: true,
            api_port: API_PORT_DEFAULT.into(),
            i2c_door: true,
            device_tree: false, // off-Pi: the I2C check is silent (ref:273)
            now,
        }
    }

    #[test]
    fn a_quiet_body_is_well() {
        let (svc, bus, sens) = (all_active(), BusStatus::new(), SensorReadings::new());
        let i = inputs(&svc, &bus, &sens, 1_000.0);
        let (rows, cond) = evaluate(&i);
        assert!(rows.is_empty(), "{rows:?}");
        assert_eq!(cond, condition::WELL);
    }

    #[test]
    fn a_fresh_fault_row_makes_the_sweep_hurt() {
        let mut svc = all_active();
        svc.insert("loa-oled".into(), (SvcState::Failed, true));
        let (bus, sens) = (BusStatus::new(), SensorReadings::new());
        let i = inputs(&svc, &bus, &sens, 1_000.0);
        let (rows, cond) = evaluate(&i);
        assert_eq!(cond, condition::HURTS);
        assert_eq!(rows[0].code, "OLED DOWN");
        assert_eq!(rows[0].level, FAULT);
        assert_eq!(rows[0].text, "loa-oled is failed");
    }

    #[test]
    fn warn_only_niggles() {
        let (svc, bus) = (all_active(), BusStatus::new());
        let mut sens = SensorReadings::new();
        sens.insert("temp_c".into(), (Some(23.8), Some(1_000.0 - 700.0))); // older than 600
        let mut i = inputs(&svc, &bus, &sens, 1_000.0);
        i.disk = Some((90, 3.0, 30.0));
        let (rows, cond) = evaluate(&i);
        assert_eq!(cond, condition::NIGGLE);
        assert!(rows.iter().all(|r| r.level == WARN));
        assert!(rows.iter().any(|r| r.code == "TEMP STALE"));
        assert!(rows.iter().any(|r| r.code == "DISK 90%"));
    }

    #[test]
    fn mute_outranks_hurts_a_stale_report_never_reads_calm() {
        let rep = Report::new(0.0, Some("abcd1234".into()), vec![Row::new(FAULT, "OLED DOWN".into(), "loa-oled is failed".into())]);
        // rows say hurts; the report is older than FAULTS_STALE_S.
        assert_eq!(condition(Some(&rep), FAULTS_STALE_S + 1.0), condition::MUTE);
        // exactly at the mark it is still believed (the reference uses strict >)
        assert_eq!(condition(Some(&rep), FAULTS_STALE_S - 0.5), condition::HURTS);
        // never swept at all: silence is not health
        assert_eq!(condition(None, 1_000.0), condition::MUTE);
    }

    #[test]
    fn a_stale_report_with_only_warns_is_still_mute() {
        let rep = Report::new(0.0, None, vec![Row::new(WARN, "DISK 85%".into(), "x".into())]);
        assert_eq!(condition(Some(&rep), 10_000.0), condition::MUTE);
    }

    #[test]
    fn the_faults_file_reader_agrees_with_the_row_writer() {
        // The CLI's --report path derives condition from the cached file; a
        // report whose ts is fresh keeps its verdict, an old one goes mute —
        // the same function, the same rule, tested through Report::new.
        let (svc, bus, sens) = (all_active(), BusStatus::new(), SensorReadings::new());
        let (rows, _) = evaluate(&inputs(&svc, &bus, &sens, 500.0));
        let rep = Report::new(500.0, Some("b00t".into()), rows);
        assert_eq!(condition(Some(&rep), 500.0), condition::WELL);
        assert_eq!(condition(Some(&rep), 900.0), condition::MUTE); // 400 s old
    }

    #[test]
    fn a_crash_looping_unit_is_a_loop_not_a_down() {
        for st in [SvcState::Looping] {
            let mut svc = all_active();
            svc.insert("loa-weather".into(), (st.clone(), true));
            let (bus, sens) = (BusStatus::new(), SensorReadings::new());
            let (rows, _) = evaluate(&inputs(&svc, &bus, &sens, 0.0));
            assert_eq!(rows.len(), 1);
            assert_eq!(rows[0].code, "WEATHER LOOP");
            assert!(rows[0].text.contains("CRASH-LOOPING"));
            assert_eq!(rows[0].level, FAULT);
        }
    }

    #[test]
    fn every_roster_unit_that_is_down_gets_its_own_row() {
        let (bus, sens) = (BusStatus::new(), SensorReadings::new());
        for unit in UNITS {
            let mut svc = all_active();
            svc.insert(unit.into(), (SvcState::Failed, true));
            let (rows, cond) = evaluate(&inputs(&svc, &bus, &sens, 0.0));
            let down = rows.iter().find(|r| r.code.ends_with(" DOWN")).unwrap();
            assert_eq!(down.code, format!("{} DOWN", short(unit)));
            assert_eq!(cond, condition::HURTS, "{unit}");
        }
        // loa-fault itself is on the roster (the seventh), and its short is FAULT.
        assert!(UNITS.contains(&"loa-fault"));
        assert_eq!(short("loa-fault"), "FAULT");
    }

    #[test]
    fn the_ring_inactive_row_is_a_warn_not_a_fault() {
        let mut svc = all_active();
        svc.insert("loa-ring".into(), (SvcState::Inactive, true));
        let (bus, sens) = (BusStatus::new(), SensorReadings::new());
        let (rows, cond) = evaluate(&inputs(&svc, &bus, &sens, 0.0));
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].code, "RING STOPPED");
        assert_eq!(rows[0].level, WARN);
        assert!(rows[0].text.contains("present but stopped"));
        assert_eq!(cond, condition::NIGGLE);
    }

    #[test]
    fn an_unknown_state_downs_the_unit() {
        // systemctl printed rubbish (a missing binary lands here too: empty
        // stdout parses Unknown upstream, ref:93's `out or "unknown"`).
        let mut svc = all_active();
        svc.insert("loa-motion".into(), (SvcState::Unknown, false));
        let (bus, sens) = (BusStatus::new(), SensorReadings::new());
        let (rows, _) = evaluate(&inputs(&svc, &bus, &sens, 0.0));
        assert_eq!(rows[0].code, "MOTION DOWN");
        assert_eq!(rows[0].text, "loa-motion is unknown");
    }

    #[test]
    fn bus_clash_when_the_wrong_hand_holds_the_face_bus() {
        let mut bus = BusStatus::new();
        bus.insert(
            "/dev/spidev0.0".into(),
            (true, vec!["root  12345 F. mq  /dev/spidev0.0".to_string()]),
        );
        bus.insert(
            "/dev/spidev1.0".into(),
            (true, vec!["flatline  999 F. mq  /dev/spidev1.0".to_string()]),
        );
        let (svc, sens) = (all_active(), SensorReadings::new());
        let (rows, cond) = evaluate(&inputs(&svc, &bus, &sens, 0.0));
        assert_eq!(cond, condition::HURTS);
        assert_eq!(rows.iter().filter(|r| r.code == "BUS CLASH").count(), 2);
        assert!(rows[0].text.contains("held by something else"));
    }

    #[test]
    fn a_bus_held_by_its_right_owner_is_quiet() {
        let mut bus = BusStatus::new();
        bus.insert(
            "/dev/spidev0.0".into(),
            (true, vec!["flatline  42 F. mq  /dev/spidev0.0  loa-oled".to_string()]),
        );
        bus.insert("/dev/spidev1.0".into(), (false, vec![])); // no node: not a fault
        let (svc, sens) = (all_active(), SensorReadings::new());
        let (rows, cond) = evaluate(&inputs(&svc, &bus, &sens, 0.0));
        assert!(rows.is_empty(), "{rows:?}");
        assert_eq!(cond, condition::WELL);
    }

    #[test]
    fn an_unheld_bus_goes_dark_with_its_label() {
        let mut bus = BusStatus::new();
        bus.insert("/dev/spidev0.0".into(), (true, vec![])); // node, no holders
        bus.insert("/dev/spidev1.0".into(), (true, vec![]));
        let (svc, sens) = (all_active(), SensorReadings::new());
        let (rows, _) = evaluate(&inputs(&svc, &bus, &sens, 0.0));
        let codes: Vec<&str> = rows.iter().map(|r| r.code.as_str()).collect();
        assert!(codes.contains(&"FACE DARK"), "{codes:?}");
        assert!(codes.contains(&"RING DARK"), "{codes:?}");
        let face = rows.iter().find(|r| r.code == "FACE DARK").unwrap();
        assert!(face.text.contains("nothing is driving /dev/spidev0.0"));
        assert!(face.text.contains("so the face is dark"));
    }

    #[test]
    fn disk_warns_at_85_and_not_before() {
        // The reference's only tier: >= 85 (ref:251). 84 is silent.
        let (svc, bus, sens) = (all_active(), BusStatus::new(), SensorReadings::new());
        let mut i = inputs(&svc, &bus, &sens, 0.0);
        i.disk = Some((84, 4.5, 30.0));
        let (rows, _) = evaluate(&i);
        assert!(rows.is_empty(), "{rows:?}");
        i.disk = Some((85, 4.4, 30.0));
        let (rows, cond) = evaluate(&i);
        assert_eq!(cond, condition::NIGGLE);
        assert_eq!(rows[0].code, "DISK 85%");
        assert!(rows[0].text.contains("4.4 GB free"));
        // even 90 is still a warn — there is no fault tier to reach (ref:244-254)
        i.disk = Some((97, 0.9, 30.0));
        let (rows, cond) = evaluate(&i);
        assert_eq!(cond, condition::NIGGLE);
        assert_eq!(rows[0].level, WARN);
    }

    #[test]
    fn a_stale_sensor_is_a_ghost_and_warns() {
        let (svc, bus) = (all_active(), BusStatus::new());
        let mut sens = SensorReadings::new();
        sens.insert("temp_c".into(), (Some(23.8), Some(0.0))); // now = 700: age 700 s
        let (rows, _) = evaluate(&inputs(&svc, &bus, &sens, 700.0));
        assert_eq!(rows[0].code, "TEMP STALE");
        assert_eq!(rows[0].level, WARN);
        assert_eq!(rows[0].face, "TEMP 11M OLD"); // int(700/60) truncates
        assert!(rows[0].text.contains("ghost"));
        // a fresh reading is silent
        sens.insert("temp_c".into(), (Some(23.8), Some(650.0)));
        let (rows, _) = evaluate(&inputs(&svc, &bus, &sens, 700.0));
        assert!(rows.is_empty(), "{rows:?}");
    }

    #[test]
    fn a_value_with_no_timestamp_cannot_be_believed() {
        let (svc, bus) = (all_active(), BusStatus::new());
        let mut sens = SensorReadings::new();
        sens.insert("pressure_hpa".into(), (Some(1013.2), None));
        let (rows, cond) = evaluate(&inputs(&svc, &bus, &sens, 0.0));
        assert_eq!(cond, condition::NIGGLE);
        assert_eq!(rows[0].code, "BARO NO TS");
    }

    #[test]
    fn rails_name_the_cause_not_the_bits() {
        let (svc, bus, sens) = (all_active(), BusStatus::new(), SensorReadings::new());
        let mut i = inputs(&svc, &bus, &sens, 0.0);
        // 0x1 under-voltage only: one row, UNDERVOLT
        i.throttled = Some(0x1);
        let (rows, _) = evaluate(&i);
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].code, "UNDERVOLT");
        assert!(rows[0].text.contains("(throttled=0x1)"));
        // under-voltage WITH throttling response: still one row, honest addendum
        i.throttled = Some(0xF);
        let (rows, _) = evaluate(&i);
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].code, "UNDERVOLT");
        assert!(rows[0].text.contains("sagging RIGHT NOW — SoC throttled to cope"));
        // throttled (bit 3 = arm throttled, no 5V problem), hot: HOT + temp on face
        i.throttled = Some(0xC);
        i.temp_val = Some(84.5);
        let (rows, _) = evaluate(&i);
        assert_eq!(rows[0].code, "HOT");
        assert_eq!(rows[0].face, "HOT 84C"); // {:.0} rounds half-to-even, like Python's
        assert!(rows[0].text.contains("SoC at 84.5C"));
        // throttled, cool: THROTTLED
        i.throttled = Some(0x2); // frequency capped, not hot
        i.temp_val = Some(50.0);
        let (rows, _) = evaluate(&i);
        assert_eq!(rows[0].code, "THROTTLED");
        assert_eq!(rows[0].face, "THROTTLED");
        // throttled, temperature unreadable: HOT is the honest guess (ref:199)
        i.throttled = Some(0x2);
        i.temp_val = None;
        let (rows, _) = evaluate(&i);
        assert_eq!(rows[0].code, "HOT");
        assert!(rows[0].text.contains("temperature unreadable"));
        // off-Pi: no throttled bits, no rails rows at all
        i.throttled = None;
        let (rows, cond) = evaluate(&i);
        assert!(rows.is_empty(), "{rows:?}");
        assert_eq!(cond, condition::WELL);
    }

    #[test]
    fn api_dead_when_the_unit_stops_or_the_door_is_not_listening() {
        let (svc, bus, sens) = (all_active(), BusStatus::new(), SensorReadings::new());
        let mut i = inputs(&svc, &bus, &sens, 0.0);
        // unit active but the door is closed
        i.api_listening = false;
        let (rows, cond) = evaluate(&i);
        assert_eq!(cond, condition::HURTS);
        assert_eq!(rows[0].code, "API DEAD");
        assert!(rows[0].text.contains("cortex not answering on :8765"));
        assert!(rows[0].text.contains("(unit 'active', port closed)"));
        // door listening, cortex unit dead: the reference re-reads the unit
        // verbatim (ref:262) — a rubbish word lands in the row's quotes.
        i.api_listening = true;
        i.cortex_state = "inactive".into();
        let (rows, _) = evaluate(&i);
        assert_eq!(rows[0].code, "API DEAD");
        assert!(rows[0].text.contains("(unit 'inactive', port listening)"));
        // a moved door: the row names the port that was checked
        i.cortex_state = "active".into();
        i.api_listening = false;
        i.api_port = "9999".into();
        let (rows, _) = evaluate(&i);
        assert!(rows[0].text.contains(":9999"));
    }

    #[test]
    fn i2c_without_a_door_is_a_warn_on_pi_only() {
        let (svc, bus, sens) = (all_active(), BusStatus::new(), SensorReadings::new());
        let mut i = inputs(&svc, &bus, &sens, 0.0);
        i.i2c_door = false;
        // no device-tree: off-Pi (a bench box) — the check is silent (ref:273)
        let (rows, cond) = evaluate(&i);
        assert!(rows.is_empty(), "{rows:?}");
        assert_eq!(cond, condition::WELL);
        // a Pi with i2c-dev not loaded (ref:270-276): warn
        i.device_tree = true;
        let (rows, cond) = evaluate(&i);
        assert_eq!(cond, condition::NIGGLE);
        assert_eq!(rows[0].code, "I2C NO DOOR");
        assert_eq!(rows[0].level, WARN);
    }

    #[test]
    fn faults_sort_before_warns() {
        let mut svc = all_active();
        svc.insert("loa-weather".into(), (SvcState::Failed, true));
        let bus = BusStatus::new();
        let mut sens = SensorReadings::new();
        sens.insert("temp_c".into(), (Some(1.0), Some(-1000.0))); // ancient -> warn
        let mut i = inputs(&svc, &bus, &sens, 0.0);
        i.disk = Some((90, 1.0, 30.0));
        let (rows, cond) = evaluate(&i);
        assert_eq!(cond, condition::HURTS);
        assert_eq!(rows[0].level, FAULT);
        assert!(rows[1..].iter().all(|r| r.level == WARN));
        assert_eq!(rows[0].code, "WEATHER DOWN");
    }

    #[test]
    fn every_emitted_code_lives_in_the_const_list() {
        // Force every branch on at once: no check may emit a code outside
        // CODES (an invented word the glass and the console never agreed on).
        let mut svc = HashMap::new();
        for u in UNITS {
            svc.insert(u.to_string(), (SvcState::Unknown, false));
        }
        svc.insert("loa-ring".into(), (SvcState::Inactive, true));
        let mut bus = BusStatus::new();
        bus.insert(
            "/dev/spidev0.0".into(),
            (true, vec!["someone 1 F. /dev/spidev0.0".into()]),
        );
        bus.insert("/dev/spidev1.0".into(), (true, vec![]));
        let mut sens = SensorReadings::new();
        sens.insert("temp_c".into(), (Some(1.0), None));
        sens.insert("pressure_hpa".into(), (Some(1.0), Some(0.0)));
        let mut i = inputs(&svc, &bus, &sens, 10_000.0);
        i.disk = Some((99, 0.3, 30.0));
        i.throttled = Some(0x1);
        i.cortex_state = "failed".into();
        i.api_listening = false;
        i.i2c_door = false;
        i.device_tree = true;
        let (rows, cond) = evaluate(&i);
        assert_eq!(cond, condition::HURTS);
        let matches_template = |code: &str| {
            CODES.iter().any(|t| match *t {
                "RING STOPPED" | "BUS CLASH" | "UNDERVOLT" | "THROTTLED" | "HOT" | "API DEAD"
                | "I2C NO DOOR" => code == *t,
                "{SHORT} DOWN" => code.ends_with(" DOWN"),
                "{SHORT} LOOP" => code.ends_with(" LOOP"),
                "{LABEL} DARK" => code.ends_with(" DARK"),
                "{LABEL} NO TS" => code.ends_with(" NO TS"),
                "{LABEL} STALE" => code.ends_with(" STALE"),
                "DISK {pct}%" => code.starts_with("DISK ") && code.ends_with('%'),
                _ => false,
            })
        };
        for r in &rows {
            assert!(matches_template(&r.code), "code {:?} is outside CODES", r.code);
        }
        let codes: Vec<&str> = rows.iter().map(|r| r.code.as_str()).collect();
        for expect in [
            "CORTEX DOWN", "OLED DOWN", "RING STOPPED", "MOTION DOWN", "SONAR DOWN",
            "WEATHER DOWN", "FAULT DOWN", "BUS CLASH", "RING DARK", "TEMP NO TS",
            "BARO STALE", "DISK 99%", "UNDERVOLT", "API DEAD", "I2C NO DOOR",
        ] {
            assert!(codes.contains(&expect), "{expect} missing from {codes:?}");
        }
    }

    #[test]
    fn a_new_sweep_is_never_mute() {
        let (svc, bus, sens) = (all_active(), BusStatus::new(), SensorReadings::new());
        let (rows, cond) = evaluate(&inputs(&svc, &bus, &sens, 0.0));
        assert_eq!(cond, condition::WELL);
        assert_eq!(sweep_condition(&rows), condition::WELL);
        // mute exists only in `condition`, the report-side rule — and the
        // vocabulary comes from loa-common, never re-literaled here.
        assert_eq!(condition::CONDITIONS, ["well", "niggle", "hurts", "mute"]);
    }

    #[test]
    fn svc_state_parse_is_the_references_word_list() {
        assert_eq!(SvcState::parse("active"), SvcState::Active);
        assert_eq!(SvcState::parse("inactive"), SvcState::Inactive);
        assert_eq!(SvcState::parse("failed"), SvcState::Failed);
        for w in ["activating", "auto-restart", "reloading"] {
            assert_eq!(SvcState::parse(w), SvcState::Looping, "{w}");
        }
        for w in ["", "unknown", "enabled", "error", "ACTIVE"] {
            assert_eq!(SvcState::parse(w), SvcState::Unknown, "{w}");
        }
    }

    #[test]
    fn report_counts_its_own_rows() {
        let rep = Report::new(
            1.0,
            Some("x".into()),
            vec![
                Row::new(FAULT, "A".into(), "a".into()),
                Row::new(WARN, "B".into(), "b".into()),
                Row::new(WARN, "C".into(), "c".into()),
            ],
        );
        assert_eq!((rep.faults, rep.warns), (1, 2));
    }

    #[test]
    fn format_report_is_the_reference_layout() {
        let rep = Report::new(
            0.0,
            Some("abcd1234".into()),
            vec![
                Row::new(FAULT, "OLED DOWN".into(), "loa-oled is failed".into()),
                Row::new(WARN, "DISK 85%".into(), "root filesystem 85% used (4.4 GB free)".into()),
            ],
        );
        let full = format_report(&rep, "2026-09-18 10:00 AEST", false);
        assert!(full.starts_with("HURTS — 2026-09-18 10:00 AEST  boot abcd1234"));
        // {:<5} "fault" fits exactly; {:<12} pads "OLED DOWN" by 3; then the
        // single separating space — 4 between DOWN and the text, Python-exact.
        assert!(full.contains("  fault OLED DOWN    loa-oled is failed"), "{full:?}");
        let quiet = format_report(&rep, "t", true);
        assert!(quiet.contains("OLED DOWN"));
        assert!(!quiet.contains("DISK"), "--quiet prints faults only");
        let verb = Report::new(0.0, None, vec![Row::new(WARN, "W".into(), "w".into())]);
        assert!(format_report(&verb, "t", false).starts_with("niggles"));
        let calm = Report::new(0.0, None, vec![]);
        assert!(format_report(&calm, "t", false).starts_with("quiet"));
    }
}
