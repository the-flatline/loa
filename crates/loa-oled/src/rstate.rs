//! What a frame is drawn FROM: the face's own view of the body, assembled
//! from the topics it holds. The reference handed this dict in from the
//! cortex (seal_state, faults_state, power_status were the cortex's reads);
//! on the body the daemon reads the topics itself and merges them here, last
//! message per topic. The renderer draws from this view and nothing else —
//! no files, no second reads, the same rule the cortex renderer had.
//!
//! The mute rule lives in `renderer_condition` and it is the DEVICE's own
//! knowledge now: it either received a `fault` message or it did not. That
//! replaces `fault_ts` in state, which existed only because the brain had to
//! remember what a daemon had heard.
//!
//! The merge is last-frame per topic, never a take: a reading that vanishes
//! because someone was slow is a lie about the body. The seal stays empty
//! here: nothing on the wire carries it (the vault's status.json is the
//! cortex's read, and no topic relays it), so the FRAG page draws GONE with
//! zero counts, the way the reference's seal_state drew {} for a missing
//! file. The words on the glass stay the reference's.

use std::collections::BTreeMap;
use std::time::{SystemTime, UNIX_EPOCH};

use loa_wire::pb;
use serde_json::{Map, Value};

/// `vcgencmd get_throttled` bits that matter to the PWR page: bit 0 is the
/// 5V input sagging RIGHT NOW, bit 2 is the SoC throttling to cope NOW. The
/// sticky bits (16/18) say something happened since boot; this page answers
/// whether it is happening now, so they are not read here — deliberately.
pub const THROTTLE_UV: u32 = 0x1;
pub const THROTTLE_THR: u32 = 0x4;

/// The fault sweep's staleness: 5 missed sweeps at 1/min = the sense has
/// gone deaf. The reference's FAULTS_STALE_S (loa/fault/__main__.py); the
/// Python tree owns it and is leaving, so the number lives here now. The
/// FAULT page draws SENSE DEAD with it; the takeover does NOT consult it —
/// that reads the condition word, exactly as the reference did.
pub const FAULTS_STALE_S: f64 = 300.0;

fn wall_now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// One fault row as swept. `level` is the API's sort key and `face` the
/// word the glass has room for; both are kept because the reference kept
/// both, and the page prefers `face` where it exists.
#[derive(Debug, Clone)]
pub struct FaultRow {
    pub level: String,
    pub code: String,
    pub text: String,
    pub face: String,
}

/// The sweep stored whole. `rows` arrive worst-first from the message; the
/// FAULT page re-sorts by the code string, like the reference did with
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
    /// The condition WORD state carries. The renderer's gate is fault_seen;
    /// once the sweep has been heard, this word is the answer — exactly what
    /// ring.condition did with the cortex's state.
    pub condition: String,
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
    /// The vault's public seal state, as the body publishes it on `power`
    /// (frag). Absent news is empty, and the FRAG page draws GONE with zero
    /// counts — the state a missing status.json drew in the reference. The
    /// device never reads the file itself; facts arrive.
    pub seal: Map<String, Value>,
    pub rails: BTreeMap<String, f64>,
    /// Some(_) once a `power` message was held; the PWR page's report age.
    /// None = the reference's "no file, never read" -> NO POWER REPORT.
    pub power_ts: Option<f64>,
    /// The throttle bitfield as ints, off the topic; None = never reported,
    /// which the PWR page renders as dashes rather than a zero that reads
    /// as "no faults".
    pub power_throttled: Option<u32>,
    /// The daemon's wall at the last merge. The render clock (`t`) is the
    /// caller's business; this is only what the message stream carried.
    pub clock_now: f64,
}

impl Default for RenderState {
    fn default() -> Self {
        RenderState {
            // "" renders nothing until a state arrives, the reference's
            // "mode or 'scope'" then supplies the default mode at render.
            mode: String::new(),
            text: None,
            dim: false,
            // the panel as mounted; the body's default is True, and the
            // daemon carries it until the `state` topic says otherwise
            flip: false,
            page: "sensors".into(),
            mood: "calm".into(),
            ring_state: "home".into(),
            ripperdoc: false,
            snr_on: true,
            condition: String::new(),
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

/// The ring's DARK decision (ring.ring_dark, restated for the fields the
/// face actually reads): the sweep names RING DARK by putting a row with
/// that CODE in the last fault report. The code, not a ring_state guess —
/// the sweep is the one that watched the bus answer.
pub fn ring_dark(st: &RenderState) -> bool {
    st.fault
        .as_ref()
        .is_some_and(|f| f.rows.iter().any(|r| r.code == "RING DARK"))
}

/// The face's view of the condition vocabulary, from THIS device's
/// knowledge: silence is not health (ring.condition — mute until a fault
/// message has arrived at all); once the sweep has been heard the word is
/// state's own. A held view that has never seen state carries an empty
/// condition, and the reference's `st.get("condition") or "well"` says well.
pub fn renderer_condition(st: &RenderState) -> &str {
    if !st.fault_seen {
        return loa_common::condition::MUTE;
    }
    if st.condition.is_empty() {
        return loa_common::condition::WELL;
    }
    &st.condition
}

/// The reference's face_state(): the face's view of the state, with the
/// takeover applied. Takes over an IDLE face — anything but the bench's own
/// ripperdoc pages, because at the bench you are driving the pages and it
/// must not fight you — but if the RING IS DARK it takes over regardless:
/// when the eyes are out, the mouth has to speak or the body has no channel
/// left. This override is render-side; state is not written back.
pub fn face_state(mut st: RenderState) -> RenderState {
    let cond = renderer_condition(&st);
    let dark = ring_dark(&st);
    if cond == loa_common::condition::HURTS || cond == loa_common::condition::MUTE || dark {
        if st.mode != "ripperdoc" || dark {
            st.mode = "ripperdoc".into();
            st.page = "fault".into();
        }
    }
    st
}

impl RenderState {
    /// Merge one decoded topic into the held view: last-frame per topic,
    /// never a take. Absent fields keep the default their message carries;
    /// a reading an update does not mention is the subject's business (see
    /// the sonar arm: a ping without a distance keeps the held reading).
    pub fn hold(&mut self, topic: &str, env: &pb::Envelope) {
        self.clock_now = wall_now();
        let Some(body) = env.body.as_ref() else {
            return;
        };
        match (topic, body) {
            // The DOWN leg is an aliased face-less Ripperdoc. Absent
            // fields are no news about that field — the held value rides,
            // the last-frame rule; the cortex sends every leg every time,
            // so absence means an old or partial publisher, not a reset.
            ("state", pb::envelope::Body::Ripperdoc(m)) => {
                if let Some(v) = &m.oled_mode {
                    self.mode = v.clone();
                }
                if let Some(v) = &m.oled_text {
                    self.text = Some(v.clone());
                } else {
                    // absent is the state's None: no slogan is the fact here,
                    // not the last one
                    self.text = None;
                }
                if let Some(v) = m.oled_dim {
                    self.dim = v;
                }
                // absent flip = the setting as last held: a message that
                // forgot it is not a command to spin the glass
                self.flip = m.oled_flip.unwrap_or(self.flip);
                if let Some(v) = &m.page {
                    self.page = v.clone();
                }
                if let Some(v) = &m.mood {
                    self.mood = v.clone();
                }
                if let Some(v) = &m.ring_state {
                    self.ring_state = v.clone();
                }
                if let Some(v) = m.ripperdoc {
                    self.ripperdoc = v;
                }
                if let Some(v) = m.snr_on {
                    self.snr_on = v;
                }
                if let Some(v) = &m.condition {
                    self.condition = v.clone();
                }
            }
            ("pir", pb::envelope::Body::Pir(m)) => {
                self.pir_high = m.high.unwrap_or(false);
                self.pir_count = m.count.unwrap_or(0) as i64;
                self.pir_last_ts = m.last_ts;
                // hold semantics the wire carries whole, so keep them whole
                self.pir_on_ts = m.on_ts;
                self.pir_last_hold = m.last_hold.unwrap_or(0.0);
            }
            ("sonar", pb::envelope::Body::Sonar(m)) => {
                // A message without a distance is a ping, not a lost
                // reading: the held value stays on the page.
                if let Some(cm) = m.cm {
                    self.snr_cm = Some(cm);
                    self.snr_count = m.count.unwrap_or(0) as i64;
                    self.snr_ts = m.ts;
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
                    ts: m.ts.unwrap_or(0.0),
                });
            }
            ("power", pb::envelope::Body::Power(m)) => {
                self.power_ts = m.ts;
                self.rails.clear(); // full-shape news: the map is the whole truth
                for (k, v) in &m.rails {
                    self.rails.insert(k.clone(), *v);
                }
                // The seal crosses as a string map (a protobuf map is one
                // value type); the FRAG page reads a typed one. Convert on
                // arrival so the page sees the shape it wants and the wire
                // keeps the shape a map can hold. A value that is neither a
                // number, a bool, nor parseable stays a string.
                self.seal.clear();
                for (k, v) in &m.frag {
                    self.seal.insert(k.clone(), seal_value(v));
                }
                // The throttle bits ride the rails map as a double
                // (983040.0); read them as ints, then keep them out of the
                // rail rows (the PWR page lists them separately).
                if let Some(bits) = self.rails.remove("throttled") {
                    self.power_throttled = Some(bits as u32);
                }
            }
            _ => {}
        }
    }

    /// The sweep's view for the FAULT page (the reference's faults_state):
    /// the report's own ts, rows counted by level, and the rows re-sorted by
    /// code string — probe readings first, in probe order, names after
    /// (sorted() on the code is a string sort). The page measures the age
    /// against the draw clock, as `t - (f.get("ts") or t)` did. None = NO
    /// SWEEP, never an invented ALL QUIET.
    pub fn faults_view(&self) -> Option<(f64, usize, usize, Vec<FaultRow>)> {
        let f = self.fault.as_ref()?;
        let mut rows = f.rows.clone();
        rows.sort_by(|a, b| a.code.cmp(&b.code)); // stable, like Python's sorted()
        let n_fault = rows.iter().filter(|r| r.level == "fault").count();
        let n_warn = rows.iter().filter(|r| r.level == "warn").count();
        Some((f.ts, n_fault, n_warn, rows))
    }
}

/// A unix-epoch seconds as local "HH:MM:SS" — the pages' clock line. std has
/// no tz database, so the offset is the process's TZ if it spells a fixed
/// one ("+10", "UTC"); a zone NAME (Australia/Sydney) resolves through
/// /etc/localtime's TZif, and to UTC if neither is readable. The label says
/// what it can prove.
pub fn fmt_clock(ts: f64) -> String {
    let secs = ts.floor() as i64 + local_offset_secs();
    let r = secs.rem_euclid(86400);
    format!("{:02}:{:02}:{:02}", r / 3600, (r % 3600) / 60, r % 60)
}

fn local_offset_secs() -> i64 {
    static OFFSET: std::sync::OnceLock<i64> = std::sync::OnceLock::new();
    *OFFSET.get_or_init(|| {
        if let Some(v) = std::env::var("TZ").ok().and_then(|t| parse_fixed_offset(&t)) {
            return v;
        }
        tzif_std_offset().unwrap_or(0)
    })
}

/// "+10", "-05:30", "UTC", "GMT", "Z" — the spellings a fixed offset takes.
fn parse_fixed_offset(tz: &str) -> Option<i64> {
    let t = tz.trim();
    if t.eq_ignore_ascii_case("UTC") || t.eq_ignore_ascii_case("GMT") || t == "Z" || t.is_empty() {
        return Some(0);
    }
    let sign = match t.as_bytes().first()? {
        b'+' => 1,
        b'-' => -1,
        _ => return None,
    };
    let body = &t[1..];
    let mut parts = body.split(':');
    let h: i64 = parts.next()?.parse().ok()?;
    let m: i64 = parts.next().unwrap_or("0").parse().ok()?;
    Some(sign * (h * 3600 + m * 60))
}

/// /etc/localtime is a TZif blob (RFC 8536). The v1 header is 44 bytes:
/// magic(4) version(1) unused(15) then six u32 counts — ttisut, ttisstd,
/// leap, time, type, char. The ttinfo array sits right after the 44-byte
/// header; the first ttinfo with its isdst bit clear is LOCAL's standard
/// offset. v2+ appends a second header with a 64-bit block after the v1
/// data; the offsets agree between blocks for standard time, so v1's is
/// read and the version question never comes up.
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
        let o = pos + i * 6;
        if o + 6 > blob.len() {
            return None;
        }
        let gmtoff =
            i32::from_be_bytes([blob[o], blob[o + 1], blob[o + 2], blob[o + 3]]) as i64;
        let isdst = blob[o + 4];
        if isdst == 0 {
            return Some(gmtoff);
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

    /// A Fault envelope. `cond` is what the SWEEP says; renderer_condition
    /// reads the word from STATE, so the takeover tests set both.
    fn fault_env(levels_codes: Vec<(&str, &str)>, cond: Option<&str>, ts: f64) -> pb::Envelope {
        pb::Envelope {
            schema_version: loa_wire::SCHEMA_VERSION,
            body: Some(pb::envelope::Body::Fault(pb::Fault {
                rows: levels_codes
                    .into_iter()
                    .map(|(level, code)| pb::fault::Row {
                        level: level.into(),
                        code: code.into(),
                        text: String::new(),
                        face: Some(code.into()),
                    })
                    .collect(),
                condition: cond.map(String::from),
                ts: Some(ts),
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
    fn once_heard_the_word_is_states() {
        let mut st = RenderState::default();
        st.hold(
            "fault",
            &fault_env(vec![("fault", "RING DARK")], Some("hurts"), 1.0),
        );
        st.hold("state", &ripperdoc(|m| m.condition = Some("well".into())));
        // ring.condition reads state's word, gated on fault_ts (here:
        // fault_seen). A stale `well` rides the takeover rule in face_state
        // — the condition is well, the DARK row still forces the page.
        assert_eq!(renderer_condition(&st), loa_common::condition::WELL);
        assert!(ring_dark(&st));
        let v = face_state(st);
        assert_eq!((v.mode.as_str(), v.page.as_str()), ("ripperdoc", "fault"));

        // state says hurts: hurts it is
        let mut st = RenderState::default();
        st.hold("fault", &fault_env(vec![], Some("well"), 1.0));
        st.hold("state", &ripperdoc(|m| m.condition = Some("hurts".into())));
        assert_eq!(renderer_condition(&st), loa_common::condition::HURTS);
    }

    #[test]
    fn the_takeover_rules_are_the_references_rules() {
        // hurts, idle scope face -> ripperdoc/fault
        let mut st = RenderState::default();
        st.mode = "scope".into();
        st.hold("fault", &fault_env(vec![], Some("hurts"), 1.0));
        st.hold("state", &ripperdoc(|m| m.condition = Some("hurts".into())));
        let v = face_state(st);
        assert_eq!((v.mode.as_str(), v.page.as_str()), ("ripperdoc", "fault"));

        // hurts, driving a page at the bench -> NOT taken over
        let mut st = RenderState::default();
        st.mode = "ripperdoc".into();
        st.page = "sensors".into();
        st.hold("fault", &fault_env(vec![], Some("hurts"), 1.0));
        st.hold("state", &ripperdoc(|m| m.condition = Some("hurts".into())));
        let v = face_state(st);
        assert_eq!((v.mode.as_str(), v.page.as_str()), ("ripperdoc", "sensors"));

        // mute takes over a text face: the mouth speaks when it cannot
        // name an idle mode differently
        let mut st = RenderState::default();
        st.mode = "text".into();
        st.text = Some("HI".into());
        let v = face_state(st);
        assert_eq!((v.mode.as_str(), v.page.as_str()), ("ripperdoc", "fault"));

        // well and bright: untouched
        let mut st = RenderState::default();
        st.mode = "ecg".into();
        st.hold("fault", &fault_env(vec![], Some("well"), 1.0));
        st.hold("state", &ripperdoc(|m| m.condition = Some("well".into())));
        let v = face_state(st);
        assert_eq!(v.mode, "ecg");
    }

    #[test]
    fn faults_page_sorts_by_code_string_like_python_sorted() {
        let mut st = RenderState::default();
        st.hold(
            "fault",
            &fault_env(
                vec![("warn", "30"), ("warn", "10"), ("warn", "OLED LOOP")],
                Some("niggle"),
                42.0,
            ),
        );
        let (ts, nf, nw, rows) = st.faults_view().unwrap();
        assert_eq!((ts, nf, nw), (42.0, 0, 3));
        assert_eq!(
            rows.iter().map(|r| r.code.as_str()).collect::<Vec<_>>(),
            ["10", "30", "OLED LOOP"],
            "digits sort as strings, names last — sorted(key=code), exactly"
        );
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
        // 0xF0004: throttled NOW (bit 2) plus the sticky since-boot bits —
        // the value a sagging board actually reports
        rails.insert("throttled".to_string(), 983044.0f64);
        let mut st = RenderState::default();
        st.hold(
            "power",
            &pb::Envelope {
                schema_version: loa_wire::SCHEMA_VERSION,
                body: Some(pb::envelope::Body::Power(pb::Power {
                    rails,
                    ts: Some(5.0),
                    ..Default::default()
                })),
            },
        );
        assert_eq!(st.power_throttled, Some(983044));
        assert_eq!(st.power_throttled.unwrap() & THROTTLE_UV, 0);
        assert_eq!(st.power_throttled.unwrap() & THROTTLE_THR, THROTTLE_THR);
        assert_eq!(st.rails.len(), 1, "throttled is not a rail row");
        assert_eq!(st.power_ts, Some(5.0));
    }
}

/// A frag-map string back into the typed shape the FRAG page reads: bools
/// by word, numbers by parse, everything else as the string it is. The
/// wire's map has one value type; this is the only place that remembers
/// which field was which kind.
pub fn seal_value(s: &str) -> Value {
    match s {
        "true" => Value::Bool(true),
        "false" => Value::Bool(false),
        "null" => Value::Null,
        other => other.parse::<f64>().map(Value::from).unwrap_or_else(|_| Value::String(s.into())),
    }
}

#[cfg(test)]
mod seal_tests {
    use super::seal_value;

    #[test]
    fn wire_strings_become_the_typed_shapes_the_page_reads() {
        // The FRAG page asks for sealed as bool, counts as numbers, and the
        // hash as a string — all crossed as one map<string,string>.
        assert_eq!(seal_value("true").as_bool(), Some(true));
        assert_eq!(seal_value("false").as_bool(), Some(false));
        assert_eq!(seal_value("7").as_f64(), Some(7.0));
        assert_eq!(seal_value("null"), serde_json::Value::Null);
        assert_eq!(
            seal_value("5d41402abc4b2a76").as_str(),
            Some("5d41402abc4b2a76")
        );
        assert_eq!(
            seal_value("SEALED 2026-09-18 / OPENED BY test").as_str(),
            Some("SEALED 2026-09-18 / OPENED BY test")
        );
    }
}
