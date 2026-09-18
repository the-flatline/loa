//! What a frame is drawn FROM: the face's own view of the body, assembled
//! from the topics it holds. The reference handed this dict in from the
//! cortex (seal_state, faults_state, power_status were the cortex's file
//! reads); here the same fields are filled from HELD MESSAGES and nothing is
//! read off a disk.
//!
//! A renderer that reads a file or another service draws something other
//! than the state it was given, and the failure is always the same shape: a
//! plausible, wrong picture. So this module has no file reads at all — the
//! topics ARE the input, and `hold` is the one place they merge.
//!
//! `state` arrives as the topic's alias of the face-less `Ripperdoc` (see
//! loa-wire's ALIASES): the same shape, no pixels in it. Timestamps: every
//! subject message carries its own `ts` (the reference's state.py did the
//! same — pir_last_ts, snr_ts, faults.ts), so the age strings are deltas of
//! two numbers off the wire. `clock_now` is the daemon's wall at merge time,
//! which is what the reference's time.time() was in the cortex's process.

use std::collections::BTreeMap;
use std::time::{SystemTime, UNIX_EPOCH};

use loa_wire::pb;
use serde_json::{Map, Value};

/// `vcgencmd get_throttled` bits, and what they mean. Bit 0 is the 5V input
/// sagging RIGHT NOW, bit 2 is the SoC throttling to cope NOW; bits 1 and 3
/// are the response and the heat that already happened. Named here because
/// the PWR page reads them.
pub const THROTTLE_UV: u32 = 0x1;
pub const THROTTLE_THR: u32 = 0x4;

/// The fault sweep's staleness: 5 missed sweeps at 1/min = the sense has
/// gone deaf. The reference's FAULTS_STALE_S; the Python tree owns it and is
/// leaving, so the number lives here now.
pub const FAULTS_STALE_S: f64 = 300.0;

/// The states where the ring draws NOTHING (ring.DARK_STATES). The face
/// takes over the mouth when the eyes are out — the takeover rule needs this
/// set, and it is the ring's vocabulary, restated here because the renderer
/// is the consumer. If a face takeover changes, ring.py's DARK_STATES is the
/// other half of the decision.
pub const RING_DARK_STATES: [&str; 7] =
    ["off", "darksweep", "alarm", "hurts", "mute", "glitch", "dead"];

fn wall_now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// One row of the sweep: `level` is the machine word, `face` the evidence
/// the glass shows (HOT is a name, "HOT 86C" is something you can act on).
#[derive(Debug, Clone)]
pub struct FaultRow {
    pub level: String,
    pub code: String,
    pub text: String,
    pub face: String,
}

/// The sweep stored whole. `rows` arrive worst-first from the message; the
/// FRAG/FAULT pages re-sort by the code string, like the reference did with
/// `sorted(rows, key=lambda r: r.get("code"))`.
#[derive(Debug, Clone)]
pub struct FaultMsg {
    pub rows: Vec<FaultRow>,
    pub condition: Option<String>,
    pub ts: f64,
}

/// The merged view: presentation fields, held readings, the fault rows, the
/// seal, the rails. The renderer reads this and nothing else.
#[derive(Debug, Clone)]
pub struct RenderState {
    // -- presentation (from the `state` topic) --
    pub mode: String,
    pub text: Option<String>,
    pub dim: bool,
    pub flip: bool,
    pub page: String,
    pub mood: String,
    pub ring_state: String,
    pub ripperdoc: bool,
    pub snr_on: bool,
    // -- held readings (from pir / sonar / weather) --
    pub pir_high: bool,
    pub pir_count: i64,
    pub pir_last_ts: Option<f64>,
    pub pir_on_ts: Option<f64>,
    pub pir_last_hold: f64,
    pub snr_cm: Option<f64>,
    pub snr_count: i64,
    pub snr_ts: Option<f64>,
    pub temp_c: Option<f64>,
    pub hum_pct: Option<f64>,
    pub pressure_hpa: Option<f64>,
    pub baro_trend: String,
    // -- the sweep (from `fault`), stored whole --
    pub fault: Option<FaultMsg>,
    /// A `fault` message received at all, whatever it contains: the device's
    /// own knowledge that the sweep exists. "silence is not health", now the
    /// body's own knowledge rather than the brain's.
    pub fault_seen: bool,
    // -- the vault and the rails --
    /// The vault's public seal state. Nothing on the wire carries it — the
    /// cortex never publishes status.json — so it is empty on this device
    /// and the FRAG page says NO SEAL STATE the way it says NO SWEEP when
    /// the file is missing: an absent fact drawn as itself, not as zero.
    pub seal: Map<String, Value>,
    pub rails: BTreeMap<String, f64>,
    /// Some(_) once a `power` message was held; the PWR page's report age.
    /// None = the reference's "no file, never read" -> NO POWER REPORT.
    pub power_ts: Option<f64>,
    /// The throttle bitfield as ints, off the topic; None = never reported,
    /// which the PWR page renders as "NO REPORT — power sense never read"
    /// rather than a zero that reads as "no faults".
    pub power_throttled: Option<u32>,
    /// The daemon's wall at the last merge; ages compute against this.
    pub clock_now: f64,
}

impl Default for RenderState {
    fn default() -> Self {
        RenderState {
            mode: String::new(),
            text: None,
            dim: false,
            flip: false,
            page: "sensors".into(),
            mood: "calm".into(),
            ring_state: "home".into(),
            ripperdoc: false,
            snr_on: true,
            pir_high: false,
            pir_count: 0,
            pir_last_ts: None,
            pir_on_ts: None,
            pir_last_hold: 0.0,
            snr_cm: None,
            snr_count: 0,
            snr_ts: None,
            temp_c: None,
            hum_pct: None,
            pressure_hpa: None,
            baro_trend: String::new(),
            fault: None,
            fault_seen: false,
            seal: Map::new(),
            rails: BTreeMap::new(),
            power_ts: None,
            power_throttled: None,
            clock_now: wall_now(),
        }
    }
}

/// The ring's DARK decision for a held view (ring.ring_dark, restated for
/// the fields the face actually reads).
pub fn ring_dark(st: &RenderState) -> bool {
    RING_DARK_STATES.contains(&st.ring_state.as_str())
        || matches!(renderer_condition(st), loa_common::condition::MUTE | loa_common::condition::HURTS)
}

/// The face's view of the condition vocabulary, from THIS device's
/// knowledge: mute until a fault message has been heard at all (reading that
/// silence as well is what let a body sit hurting with a calm face), hurts
/// when a row is at the hurts level or the sweep has gone stale, niggle on a
/// warn, well else. Precedence and words are the reference's ring.condition.
pub fn renderer_condition(st: &RenderState) -> &'static str {
    use loa_common::condition::{HURTS, MUTE, NIGGLE, WELL};
    if !st.fault_seen {
        return MUTE;
    }
    let fault = st.fault.as_ref();
    if fault
        .iter()
        .flat_map(|f| f.rows.iter())
        .any(|r| r.level == "fault")
    {
        return HURTS;
    }
    if fault.map_or(false, |f| st.clock_now - f.ts > FAULTS_STALE_S) {
        return HURTS;
    }
    if fault
        .iter()
        .flat_map(|f| f.rows.iter())
        .any(|r| r.level == "warn")
    {
        return NIGGLE;
    }
    WELL
}

/// The reference's face_state(): the face's view of the state dict, with the
/// takeover applied. Takes over an IDLE face (the bench drives the pages and
/// it must not fight you), but if the RING IS DARK the mouth speaks or the
/// body has no channel left. This override is render-side; state is not
/// written back.
pub fn face_state(mut st: RenderState) -> RenderState {
    let cond = renderer_condition(&st);
    let dark = ring_dark(&st);
    if matches!(cond, HURTS | MUTE) || dark {
        let hurts = cond == HURTS || dark;
        let idle = st.mode.is_empty() || st.mode == "off" || st.mode == "scope";
        if hurts && idle {
            st.mode = "ripperdoc".into();
            st.page = "fault".into();
        }
    }
    st
}

impl RenderState {
    /// Merge one decoded topic into the held view: last-frame per topic,
    /// never a take. A field an update does not mention keeps its held value
    /// unless the subject's contract says the message is full-shape (news
    /// carries the whole truth of its subject).
    pub fn hold(&mut self, topic: &str, env: &pb::Envelope) {
        self.clock_now = wall_now();
        let Some(body) = env.body.as_ref() else {
            return;
        };
        match (topic, body) {
            // The DOWN leg is an aliased face-less Ripperdoc.
            ("state", pb::envelope::Body::Ripperdoc(m)) => {
                if let Some(v) = &m.oled_mode {
                    self.mode = v.clone();
                }
                self.text = m.oled_text.clone();
                self.dim = m.oled_dim.unwrap_or(false);
                // absent flip = the panel as mounted (the body's default is
                // True; a message that forgot it is not a command to spin)
                self.flip = m.oled_flip.unwrap_or(self.flip);
                self.page = m.page.clone().unwrap_or_else(|| "sensors".into());
                self.mood = m.mood.clone().unwrap_or_default();
                self.ring_state = m.ring_state.clone().unwrap_or_default();
                self.ripperdoc = m.ripperdoc.unwrap_or(false);
                self.snr_on = m.snr_on.unwrap_or(true);
            }
            ("pir", pb::envelope::Body::Pir(m)) => {
                self.pir_high = m.high.unwrap_or(false);
                self.pir_count = m.count.unwrap_or(0) as i64;
                // The subject's own timestamps where it has them: age
                // strings mean the MOTION's age, not the news' age.
                self.pir_last_ts = Some(m.last_ts.or(m.ts).unwrap_or(self.clock_now));
                if self.pir_high {
                    self.pir_on_ts = Some(m.on_ts.or(m.ts).unwrap_or(self.clock_now));
                } else if let Some(h) = m.last_hold {
                    self.pir_last_hold = h;
                }
            }
            ("sonar", pb::envelope::Body::Sonar(m)) => {
                // A message without a distance is a ping, not a lost
                // reading: the held value stays on the page.
                if let Some(cm) = m.cm {
                    self.snr_cm = Some(cm);
                    self.snr_count = m.count.unwrap_or(0) as i64;
                    self.snr_ts = Some(m.ts.unwrap_or(self.clock_now));
                }
            }
            ("weather", pb::envelope::Body::Weather(m)) => {
                self.temp_c = m.temp_c;
                self.hum_pct = m.hum_pct;
                self.pressure_hpa = m.pressure_hpa;
                self.baro_trend = m.trend.clone().unwrap_or_default();
            }
            ("fault", pb::envelope::Body::Fault(m)) => {
                self.fault_seen = true;
                self.fault = Some(FaultMsg {
                    rows: m
                        .rows
                        .iter()
                        .map(|r| FaultRow {
                            level: r.level.clone(),
                            code: r.code.clone(),
                            text: r.text.clone(),
                            face: r.face.clone().unwrap_or_default(),
                        })
                        .collect(),
                    condition: m.condition.clone(),
                    ts: m.ts.unwrap_or(self.clock_now),
                });
            }
            ("power", pb::envelope::Body::Power(m)) => {
                self.power_ts = Some(m.ts.unwrap_or(self.clock_now));
                self.rails.clear(); // full-shape news: the map is the whole truth
                for (k, v) in &m.rails {
                    self.rails.insert(k.clone(), *v);
                }
                // The throttle bits ride the rails map as a double
                // (983040.0); read them as ints, then keep them out of the
                // rail rows (the PWR page lists them separately).
                if let Some(v) = self.rails.remove("throttled") {
                    self.power_throttled = Some(v as u32);
                }
            }
            _ => {}
        }
    }

    /// The sweep's view for the FRAG/FAULT pages (the reference's
    /// faults_state): age from the message's own ts, counts by level, and
    /// the rows re-sorted by code string — probe readings first, in probe
    /// order, names after. None = NO SWEEP, never an invented ALL QUIET.
    pub fn faults_view(&self) -> Option<(f64, Option<&String>, usize, usize, Vec<FaultRow>)> {
        let f = self.fault.as_ref()?;
        let mut rows = f.rows.clone();
        rows.sort_by(|a, b| a.code.cmp(&b.code)); // stable, like Python's sorted()
        let n_fault = rows.iter().filter(|r| r.level == "fault").count();
        let n_warn = rows.iter().filter(|r | r.level == "warn").count();
        Some((
            self.clock_now - f.ts,
            f.condition.as_ref(),
            n_fault,
            n_warn,
            rows,
        ))
    }
}

// ---------------------------------------------------------------------------
// The number and clock formatting the pages use. The reference's fmt_num,
// fmt_secs, fmt_pct, fmt_clock — carried across with C's %.0f semantics
// (banker's rounding) intact, because Python's round() is half-to-even and a
// frame that disagrees with the console about what 30.5 is looked like a bug
// in the glass.
// ---------------------------------------------------------------------------

/// fmt_num(value, nd): `f"{round(float(v), nd):.{nd}f}"` — correct decimal
/// rounding of the stored binary double, half-to-even. Rust's precision
/// formatting does the same rounding on the exact value, so this is not an
/// approximation of the reference: it is the reference's own rule.
pub fn fmt_num(v: f64, nd: usize) -> String {
    if v.is_nan() || v.is_infinite() {
        return "ERR".into();
    }
    format!("{v:.nd$}")
}

/// fmt_secs: <60 -> `%4.1f`, else `M:SS`. The minute number goes through
/// fmt_num's banker's rounding exactly as `fmt_num(m)` did there (an integer
/// in, so the value is untouched; the shape is kept for the tie cases).
pub fn fmt_secs(v: f64) -> String {
    if v.is_nan() || v.is_infinite() {
        return "ERR".into();
    }
    if v < 60.0 {
        // "%4.1f": width 4 so ages stack in a column
        format!("{v:>4.1}")
    } else {
        let m = fmt_num((v / 60.0).floor(), 0); // an int; the round is the reference's fmt_num(m)
        let s = (v % 60.0).floor() as i64; // int(v % 60), then %02d
        format!("{m}:{s:02}")
    }
}

/// fmt_pct: `%d%%` — two literal percent signs, like the console's.
pub fn fmt_pct(v: f64) -> String {
    format!("{}%%", fmt_num(v, 0))
}

/// A unix-epoch seconds as local "HH:MM:SS" (the reference's fmt_clock).
/// std has no tz database, so the offset is the process's TZ if it spells a
/// fixed one ("+10", "UTC"); a zone NAME (Australia/Sydney) resolves to UTC
/// unless TZif is readable. The age strings off the same messages carry the
/// meaning; this label is convenience, and it says what it can prove.
pub fn fmt_clock(ts: f64) -> String {
    let secs = ts.floor() as i64 + local_offset_secs();
    let r = secs.rem_euclid(86400);
    format!("{:02}:{:02}:{:02}", r / 3600, (r % 3600) / 60, r % 60)
}

/// Seconds east of UTC: a fixed-offset TZ, else /etc/localtime's standard
/// (non-DST) ttinfo from the TZif header, else 0. Bounded and cheap; it
/// runs at most once per rendered frame and is memoised.
fn local_offset_secs() -> i64 {
    static OFFSET: std::sync::OnceLock<i64> = std::sync::OnceLock::new();
    *OFFSET.get_or_init(|| {
        if let Some(v) = std::env::var("TZ").ok().and_then(|t| parse_fixed_offset(&t)) {
            return v;
        }
        tzif_std_offset().unwrap_or(0)
    })
}

/// Fixed-offset spellings: "+10", "-08:30", "UTC", "GMT". A zone name does
/// NOT parse here — that answer comes from TZif.
fn parse_fixed_offset(tz: &str) -> Option<i64> {
    let t = tz.trim();
    let sign = match t.as_bytes().first()? {
        b'+' => 1i64,
        b'-' => -1i64,
        _ => {
            // bare UTC/GMT is zero; anything else is a zone name for TZif
            return if t.eq_ignore_ascii_case("utc") || t.eq_ignore_ascii_case("gmt") {
                Some(0)
            } else {
                None
            };
        }
    };
    let mut parts = t[1..].split(':');
    let h: i64 = parts.next()?.parse().ok()?;
    let m: i64 = parts.next().unwrap_or("0").parse().unwrap_or(0);
    Some(sign * (h * 3600 + m * 60))
}

/// /etc/localtime is a TZif blob (RFC 8536). The v1 header is 44 bytes:
/// magic(4) version(1) unused(15) then six u32 counts — ttisut, ttisstd,
/// leap, time, type, char. The ttinfo array sits right after the transition
/// times (4B each, v1) and their type indices (1B each); each ttinfo is
/// (gmtoff i32, isdst u8, desigidx u8). The FIRST non-DST ttinfo is the
/// standard offset — the v1 block carries the same offsets as the v2 one for
/// every zone file in the wild, and this labels a clock on a face, so the
/// simplification is deliberate. Anything unreadable means UTC, visibly.
fn tzif_std_offset() -> Option<i64> {
    let blob = std::fs::read("/etc/localtime").ok()?;
    if blob.len() < 44 || &blob[0..4] != b"TZif" {
        return None;
    }
    let u32be = |o: usize| u32::from_be_bytes([blob[o], blob[o + 1], blob[o + 2], blob[o + 3]]) as usize;
    let timecnt = u32be(32);
    let typecnt = u32be(36);
    let pos = 44 + timecnt * 5; // times(4) + type indices(1), per transition
    for i in 0..typecnt {
        let at = pos + i * 6;
        if at + 6 > blob.len() {
            return None;
        }
        let gmtoff = i32::from_be_bytes([blob[at], blob[at + 1], blob[at + 2], blob[at + 3]]);
        if blob[at + 4] == 0 && (-14 * 3600..=14 * 3600).contains(&gmtoff) {
            return Some(gmtoff as i64);
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ripperdoc(f: impl FnOnce(&mut pb::Ripperdoc)) -> pb::Envelope {
        let mut m = pb::Ripperdoc::default();
        f(&mut m);
        pb::Envelope {
            schema_version: loa_wire::SCHEMA_VERSION,
            body: Some(pb::envelope::Body::Ripperdoc(m)),
        }
    }

    fn fault_env(rows: Vec<(&str, &str)>, ts: Option<f64>) -> pb::Envelope {
        pb::Envelope {
            schema_version: loa_wire::SCHEMA_VERSION,
            body: Some(pb::envelope::Body::Fault(pb::Fault {
                rows: rows
                    .into_iter()
                    .map(|(level, code)| pb::fault::Row {
                        level: level.into(),
                        code: code.into(),
                        text: String::new(),
                        face: Some(code.into()),
                    })
                    .collect(),
                condition: Some("hurts".into()),
                ts,
            })),
        }
    }

    #[test]
    fn held_state_lands_in_the_view() {
        let mut st = RenderState::default();
        st.hold(
            "state",
            &ripperdoc(|m| {
                m.oled_mode = Some("ecg".into());
                m.page = Some("sensors".into());
            }),
        );
        assert_eq!(st.mode, "ecg");
        assert_eq!(st.page, "sensors");
        assert!(st.snr_on, "absent means the body's default: on");
    }

    #[test]
    fn a_sonar_ping_without_a_distance_keeps_the_reading() {
        let mut st = RenderState::default();
        st.hold(
            "sonar",
            &pb::Envelope {
                schema_version: loa_wire::SCHEMA_VERSION,
                body: Some(pb::envelope::Body::Sonar(pb::Sonar {
                    cm: Some(123.0),
                    count: Some(4),
                    ts: Some(7.0),
                })),
            },
        );
        st.hold(
            "sonar",
            &pb::Envelope {
                schema_version: loa_wire::SCHEMA_VERSION,
                body: Some(pb::envelope::Body::Sonar(pb::Sonar::default())),
            },
        );
        assert_eq!(st.snr_cm, Some(123.0));
        assert_eq!(st.snr_ts, Some(7.0));
    }

    #[test]
    fn mute_before_the_first_fault_message() {
        let st = RenderState::default();
        assert_eq!(renderer_condition(&st), loa_common::condition::MUTE);
    }

    #[test]
    fn a_fault_row_is_hurts_and_a_warn_is_niggle() {
        let mut st = RenderState::default();
        st.hold("fault", &fault_env(vec![("fault", "10")], Some(st.clock_now)));
        assert_eq!(renderer_condition(&st), loa_common::condition::HURTS);

        let mut st = RenderState::default();
        st.hold("fault", &fault_env(vec![("warn", "20")], Some(st.clock_now)));
        assert_eq!(renderer_condition(&st), loa_common::condition::NIGGLE);

        let mut st = RenderState::default();
        st.hold("fault", &fault_env(vec![], Some(st.clock_now)));
        assert_eq!(renderer_condition(&st), loa_common::condition::WELL);
    }

    #[test]
    fn a_stale_sweep_hurts() {
        let mut st = RenderState::default();
        st.hold("fault", &fault_env(vec![], Some(100.0)));
        st.clock_now = 100.0 + FAULTS_STALE_S + 1.0;
        assert_eq!(renderer_condition(&st), loa_common::condition::HURTS);
    }

    #[test]
    fn the_takeover_rules_are_the_references_rules() {
        // hurt, idle scope face -> ripperdoc/fault
        let mut st = RenderState::default();
        st.mode = "scope".into();
        st.hold("fault", &fault_env(vec![("fault", "10")], Some(st.clock_now)));
        let v = face_state(st.clone());
        assert_eq!((v.mode.as_str(), v.page.as_str()), ("ripperdoc", "fault"));

        // hurt, driving a page at the bench -> NOT taken over
        let mut st = RenderState::default();
        st.mode = "ripperdoc".into();
        st.page = "sensors".into();
        st.hold("fault", &fault_env(vec![("fault", "10")], Some(st.clock_now)));
        let v = face_state(st.clone());
        assert_eq!((v.mode.as_str(), v.page.as_str()), ("ripperdoc", "sensors"));

        // mute with the ring DARK takes over whatever the mode was: the mouth
        // speaks when the eyes are out
        let mut st = RenderState::default();
        st.mode = "text".into();
        st.text = Some("HI".into());
        st.ring_state = "dead".into();
        let v = face_state(st.clone());
        assert_eq!((v.mode.as_str(), v.page.as_str()), ("ripperdoc", "fault"));

        // well and bright: untouched
        let mut st = RenderState::default();
        st.mode = "ecg".into();
        st.ring_state = "home".into();
        st.hold("fault", &fault_env(vec![], Some(st.clock_now)));
        let v = face_state(st.clone());
        assert_eq!(v.mode, "ecg");
    }

    #[test]
    fn faults_page_sorts_by_code_string_like_python_sorted() {
        let mut st = RenderState::default();
        st.hold(
            "fault",
            &fault_env(
                vec![("warn", "30"), ("warn", "10"), ("warn", "OLED LOOP")],
                Some(st.clock_now),
            ),
        );
        let (_, cond, nf, nw, rows) = st.faults_view().unwrap();
        assert_eq!((nf, nw), (0, 3));
        assert_eq!(cond.unwrap(), "hurts"); // carried, not recomputed
        assert_eq!(
            rows.iter().map(|r| r.code.as_str()).collect::<Vec<_>>(),
            ["10", "30", "OLED LOOP"],
            "digits sort as strings, names last — sorted(key=code), exactly"
        );
    }

    #[test]
    fn number_formatting_is_the_references_rounding() {
        assert_eq!(fmt_num(30.5, 0), "30"); // banker's, like Python's round
        assert_eq!(fmt_num(31.5, 0), "32");
        assert_eq!(fmt_num(0.5, 0), "0");
        assert_eq!(fmt_num(2.5, 0), "2");
        assert_eq!(fmt_num(293.15, 1), "293.1"); // the stored double is below
        assert_eq!(fmt_num(-0.04, 1), "-0.0");
        assert_eq!(fmt_pct(48.2), "48%%");
        assert_eq!(fmt_secs(30.5), "30.5");
        assert_eq!(fmt_secs(75.0), "1:15");
        assert_eq!(fmt_secs(900.0), "15:00");
    }

    #[test]
    fn clock_wall_is_hhmmss_and_bounded() {
        let s = fmt_clock(0.0);
        assert_eq!(s.len(), 8);
        assert_eq!(s.chars().filter(|&c| c == ':').count(), 2);
    }

    #[test]
    fn rails_merge_and_the_throttle_bits_read_as_ints() {
        let mut rails = std::collections::HashMap::new();
        rails.insert("3V3_SYS_V".to_string(), 3.29f64);
        rails.insert("throttled".to_string(), 983040.0f64);
        let mut st = RenderState::default();
        st.hold(
            "power",
            &pb::Envelope {
                schema_version: loa_wire::SCHEMA_VERSION,
                body: Some(pb::envelope::Body::Power(pb::Power {
                    rails,
                    ts: Some(5.0),
                })),
            },
        );
        assert_eq!(st.power_throttled, Some(983040));
        assert_eq!(st.power_throttled.unwrap() & THROTTLE_UV, 0);
        assert_eq!(st.power_throttled.unwrap() & THROTTLE_THR, THROTTLE_THR);
        assert_eq!(st.rails.len(), 1, "throttled is not a rail row");
        assert_eq!(st.power_ts, Some(5.0));
    }
}
