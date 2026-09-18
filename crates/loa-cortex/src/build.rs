//! state -> protobuf, per topic. The assembler that decides what each
//! message carries. Pure: it reads a State and returns a body. Whether the
//! face has pixels to ride along is the caller's business — the relay hands
//! frame bytes in, and this module assembles the state legs only.

use crate::state::{Cortex, State};
use loa_wire::pb;

pub fn build_state(st: &State) -> pb::envelope::Body {
    // The DOWN leg: presentation fields, no face. The devices render their
    // own pixels; they never receive anyone else's.
    pb::envelope::Body::Ripperdoc(pb::Ripperdoc {
        page: Some(st.page.clone()),
        mood: Some(st.mood.clone()),
        ring_state: Some(st.ring_state.clone()),
        oled_mode: Some(st.oled_mode.clone()),
        oled_text: st.oled_text.clone(),
        oled_dim: Some(st.oled_dim),
        oled_flip: Some(st.oled_flip),
        ripperdoc: Some(st.ripperdoc),
        snr_on: Some(st.snr_on),
        condition: Some(st.condition.clone()),
        face: None,
        ts: Some(crate::state::now_s()),
    })
}

/// The consumers' `ripperdoc`: the state leg PLUS whatever pixels the last
/// relayed frame carried. The face is only ever filled from a real frame —
/// the cortex has no renderer, and a number drawn on the glass arrives as
/// glass.
pub fn build_ripperdoc(st: &State, face: Option<&[u8]>) -> pb::envelope::Body {
    match build_state(st) {
        pb::envelope::Body::Ripperdoc(mut m) => {
            m.face = face.map(|f| f.to_vec());
            pb::envelope::Body::Ripperdoc(m)
        }
        _ => unreachable!("build_state returns a Ripperdoc"),
    }
}

pub fn build_ring(last_ring: Option<&[u8]>) -> pb::envelope::Body {
    pb::envelope::Body::Ring(pb::Ring {
        ring: last_ring.map(|r| r.to_vec()),
        ts: Some(crate::state::now_s()),
    })
}

/// The divergent pair, kept exactly: the FAULT message recomputes its
/// condition from the rows (so a DB DOWN the rows carry outranks a stale
/// `well` in state), while the RIPPERDOC message carries state's raw
/// condition, which may still say well. One word, two sources — the
/// renderer-side mute rule and the feed's fault view disagree on purpose
/// until a sweep lands.
pub fn build_fault(cx: &Cortex, db_down: bool) -> pb::envelope::Body {
    let mut rows = Cortex::injected_faults(db_down);
    rows.extend(cx.st.faults.iter().cloned());
    pb::envelope::Body::Fault(pb::Fault {
        rows: rows
            .iter()
            .map(|r| pb::fault::Row {
                level: r.level.clone(),
                code: r.code.clone(),
                text: r.text.clone(),
                face: r.face.clone(),
            })
            .collect(),
        condition: Some(Cortex::condition_from(&rows)),
        ts: Some(crate::state::now_s()),
    })
}

pub fn build_pir(st: &State) -> pb::envelope::Body {
    pb::envelope::Body::Pir(pb::Pir {
        high: Some(st.pir_high),
        count: Some(st.pir_count),
        last_ts: st.pir_last_ts,
        last_hold: Some(st.pir_last_hold),
        on_ts: st.pir_on_ts,
        ts: Some(crate::state::now_s()),
    })
}

pub fn build_sonar(st: &State) -> pb::envelope::Body {
    pb::envelope::Body::Sonar(pb::Sonar {
        cm: st.snr_cm,
        count: Some(st.snr_count),
        ts: Some(crate::state::now_s()),
    })
}

/// The weather, with the trend DERIVED here from the cortex's own window.
/// The pressure fields ride because Weather is the air's one message now.
pub fn build_weather(cx: &Cortex) -> pb::envelope::Body {
    let st = &cx.st;
    let (dir, series) = cx.baro_trend();
    pb::envelope::Body::Weather(pb::Weather {
        temp_c: st.temp_c,
        hum_pct: st.hum_pct,
        count: Some(st.temp_count),
        pressure_hpa: st.pressure_hpa,
        trend: Some(dir),
        series,
        ts: Some(crate::state::now_s()),
    })
}

pub fn build_init() -> pb::envelope::Body {
    pb::envelope::Body::Init(pb::Init {
        source: Some("cortex".into()),
        ts: Some(crate::state::now_s()),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::Cortex;
    use loa_wire::pb::envelope::Body;

    #[test]
    fn the_state_leg_never_carries_pixels() {
        let cx = Cortex::new();
        match build_state(&cx.st) {
            Body::Ripperdoc(m) => {
                assert!(m.face.is_none(), "down is state; up is frames");
                assert_eq!(m.page.as_deref(), Some("sensors"));
            }
            other => panic!("state must assemble a Ripperdoc, got {other:?}"),
        }
    }

    #[test]
    fn a_relayed_face_fills_the_consumer_view_and_nothing_else() {
        let cx = Cortex::new();
        // No face yet: the view is state-only, honestly blank to a viewer
        // that holds its last frame.
        match build_ripperdoc(&cx.st, None) {
            Body::Ripperdoc(m) => assert!(m.face.is_none()),
            _ => unreachable!(),
        }
        let face = [0xABu8; 1024];
        match build_ripperdoc(&cx.st, Some(&face)) {
            Body::Ripperdoc(m) => {
                assert_eq!(m.face.as_deref(), Some(&face[..]));
                assert_eq!(m.page.as_deref(), Some("sensors"), "state still rides");
            }
            _ => unreachable!(),
        }
    }

    #[test]
    fn db_down_outranks_a_stale_well_on_the_fault_leg_only() {
        let mut cx = Cortex::new();
        // state says well (no sweep has spoken); the store is down.
        match build_fault(&cx, true) {
            Body::Fault(m) => {
                assert_eq!(m.condition.as_deref(), Some("hurts"));
                assert_eq!(m.rows[0].code, "DB DOWN", "injected rows lead");
            }
            _ => unreachable!(),
        }
        // the consumer ripperdoc leg says what state says — well — while the
        // fault leg says hurts. Two views, both honest about their source.
        match build_ripperdoc(&cx.st, None) {
            Body::Ripperdoc(m) => assert_eq!(m.condition.as_deref(), Some("well")),
            _ => unreachable!(),
        }
        // with the store up and no rows at all, fault is well too.
        match build_fault(&cx, false) {
            Body::Fault(m) => assert_eq!(m.condition.as_deref(), Some("well")),
            _ => unreachable!(),
        }
    }

    #[test]
    fn the_weather_leg_derives_its_own_trend() {
        let mut cx = Cortex::new();
        // The real path: readings arrive as Weather messages, not raw pushes.
        let mk = |ts: f64, p: f64| {
            loa_wire::Msg {
                topic: "weather".into(),
                env: loa_wire::envelope(Body::Weather(pb::Weather {
                    pressure_hpa: Some(p),
                    ts: Some(ts),
                    ..Default::default()
                })),
            }
        };
        cx.merge(&mk(0.0, 1000.0));
        cx.merge(&mk(90.0, 1000.4)); // slope 16 hPa/h over the deadband
        match build_weather(&cx) {
            Body::Weather(m) => {
                assert_eq!(m.trend.as_deref(), Some("rising"));
                assert_eq!(m.series, vec![1000.0, 1000.4]);
                assert_eq!(m.pressure_hpa, Some(1000.4));
                // temp/hum untouched by a pressure-only arrival
                assert!(m.temp_c.is_none());
            }
            _ => unreachable!(),
        }
    }
}

/// The power topic: rails as read on the body. The map arrives as JSON from
/// the sampler; the wire wants string values (schema: map<string,double>).
/// An empty map still rides — "no rails readable" is a fact the FRAG page
/// draws, and a topic that goes silent looks exactly like a dead body.
pub fn build_power(rails: &serde_json::Map<String, serde_json::Value>) -> pb::envelope::Body {
    let mut m = pb::Power::default();
    m.ts = Some(crate::state::now_s());
    for (k, v) in rails {
        if let Some(f) = v.as_f64() {
            m.rails.insert(k.clone(), f);
        }
    }
    pb::envelope::Body::Power(m)
}

/// Something happened. The detail crosses the wire stringified — the schema
/// says scalars only, and a bytes field here would base64 itself into the
/// database where nobody looks for an encoding.
pub fn build_event(kind: &str, detail: &serde_json::Value, ts: f64) -> pb::envelope::Body {
    let mut m = pb::Event::default();
    m.kind = kind.into();
    m.ts = ts;
    if let Some(obj) = detail.as_object() {
        for (k, v) in obj {
            let s = match v {
                serde_json::Value::Null => continue,
                serde_json::Value::String(s) => s.clone(),
                other => other.to_string(),
            };
            m.detail.insert(k.clone(), s);
        }
    }
    pb::envelope::Body::Event(m)
}
