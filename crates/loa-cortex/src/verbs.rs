//! The three state verbs: feel, display, ripperdoc. The vault verbs live with
//! the vault; the door turns JSON into these calls and nothing else.
//!
//! A verb that changes state returns the event to log; the caller writes it
//! to the store and the `event` topic. The verb never touches a socket.

use crate::moods;
use crate::state::Cortex;

pub const PAGES: [&str; 7] =
    ["sensors", "pir", "snr", "temp", "frag", "power", "fault"];

pub const DISPLAY_MODES: [&str; 8] = [
    "scope", "ecg", "ripple", "noise", "text", "showoff", "ripperdoc", "off",
];

/// What a verb decided, for the door to render and the log to keep. The
/// payloads are the reference's event detail dicts, field for field — the
/// store and the `event` topic see exactly what they saw before.
#[derive(Debug, PartialEq)]
pub enum Logged {
    Mood { feeling: String, ring: String, note: Option<String> },
    Display { mode: Option<String>, text: Option<String>, dim: Option<bool>, flip: Option<bool> },
    Ripperdoc { on: bool, page: String },
    None,
}

/// The event detail as the reference wrote it: JSON, scalars only.
impl Logged {
    pub fn detail(&self) -> serde_json::Value {
        match self {
            Logged::Mood { feeling, note, ring } =>
                serde_json::json!({"feeling": feeling, "note": note, "ring": ring}),
            Logged::Display { mode, text, dim, flip } =>
                serde_json::json!({"mode": mode, "text": text, "dim": dim, "flip": flip}),
            Logged::Ripperdoc { on, page } => serde_json::json!({"on": on, "page": page}),
            Logged::None => serde_json::Value::Null,
        }
    }

    pub fn kind(&self) -> Option<&'static str> {
        match self {
            Logged::Mood { .. } => Some("mood"),
            Logged::Display { .. } => Some("display"),
            Logged::Ripperdoc { .. } => Some("ripperdoc"),
            Logged::None => None,
        }
    }
}

/// feel: one word in, the whole face-and-ring picture out. `next` wins when
/// both are sent (the reference silently prefers next; keeping that exactly,
/// not "fixing" it). Unknown feeling is a 400 for the door to translate.
pub fn feel(cx: &mut Cortex, feeling: Option<&str>, next: bool, note: Option<&str>) -> Result<Logged, String> {
    let mood = if next {
        moods::next_from(&cx.st.mood)
    } else {
        let f = feeling.ok_or_else(|| {
            // Both-empty is the same 400 as a bad word, exactly as the
            // reference: there is no "needs feeling or next" in its vocabulary.
            let mut names: Vec<_> = moods::MOODS.iter().map(|m| m.name).collect();
            names.sort();
            format!("feeling must be one of {}", names.join("/"))
        })?;
        moods::by_name(f).ok_or_else(|| {
            let mut names: Vec<_> = moods::MOODS.iter().map(|m| m.name).collect();
            names.sort();
            format!("feeling must be one of {}", names.join("/"))
        })?
    };
    // The ring target: one-shots are records, sustained states are state.
    // Nothing here ever clears a flag afterwards — that bug ate second
    // events in the old build.
    let logged_ring;
    if mood.one_shot {
        logged_ring = mood.ring.to_string();
        // A record rides the event topic; ring_state does not move.
    } else {
        cx.st.ring_state = mood.ring.to_string();
        logged_ring = mood.ring.to_string();
    }
    cx.st.mood = mood.name.to_string();
    cx.st.oled_mode = mood.oled_mode.to_string();
    cx.st.oled_text = mood.oled_text.map(String::from); // None clears, every mood
    cx.st.oled_dim = mood.oled_dim;
    Ok(Logged::Mood {
        feeling: mood.name.to_string(),
        ring: logged_ring,
        note: note.map(String::from),
    })
}

/// display: the face's presentation knobs. A mode change CLEARS oled_text
/// unless the new mode is text — text supplied alongside any other mode is
/// silently dropped, as in the reference. No fields at all is a no-op: no
/// state moves, no event logs.
pub fn display(
    cx: &mut Cortex,
    mode: Option<&str>,
    text: Option<&str>,
    dim: Option<bool>,
    flip: Option<bool>,
) -> Result<Logged, String> {
    // (the event logs exactly these request values — see Logged::Display)
    let mut changed = false;
    if let Some(m) = mode {
        if !DISPLAY_MODES.contains(&m) {
            return Err(format!("mode must be {}", DISPLAY_MODES.join("|")));
        }
        cx.st.oled_mode = m.to_string();
        cx.st.oled_text = if m == "text" { text.map(String::from) } else { None };
        changed = true;
    }
    if let Some(d) = dim {
        cx.st.oled_dim = d;
        changed = true;
    }
    if let Some(f) = flip {
        cx.st.oled_flip = f;
        changed = true;
    }
    if !changed {
        return Ok(Logged::None);
    }
    Ok(Logged::Display {
        mode: mode.map(String::from),
        text: text.map(String::from),
        dim,
        flip,
    })
}

/// ripperdoc: the console flag and the page. `on` also STOMPS oled_mode to
/// scope when it goes off — regardless of whatever mode was showing, which
/// is the reference's behaviour and the face forgives it because the console
/// is the operator's own surface. Page-only calls change no mode and log
/// nothing; a bad page is a 400 naming the seven that exist.
pub fn ripperdoc(
    cx: &mut Cortex,
    on: Option<bool>,
    page: Option<&str>,
) -> Result<Logged, String> {
    if let Some(p) = page {
        if !PAGES.contains(&p) {
            return Err(format!("page must be {}", PAGES.join("|")));
        }
        cx.st.page = p.to_string();
    }
    match on {
        Some(on) => {
            cx.st.ripperdoc = on;
            cx.st.oled_mode =
                if on { "ripperdoc".to_string() } else { "scope".to_string() };
            Ok(Logged::Ripperdoc { on, page: cx.st.page.clone() })
        }
        None => Ok(Logged::None),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn feel_next_walks_the_table_and_moves_sustained_ring() {
        let mut cx = Cortex::new();
        let l = feel(&mut cx, None, true, None).unwrap();
        assert_eq!(cx.st.mood, "busy");
        assert_eq!(cx.st.oled_mode, "ecg");
        assert_eq!(cx.st.ring_state, "busy", "sustained states ARE state");
        assert!(matches!(l, Logged::Mood { ref feeling, .. } if feeling == "busy"));
    }

    #[test]
    fn a_one_shot_records_without_touching_ring_state() {
        // pleased plays `scan` as a record; the ring decides at render time.
        // No flag is set, so there is nothing for a daemon to clear wrong.
        let mut cx = Cortex::new();
        feel(&mut cx, Some("pleased"), false, None).unwrap();
        assert_eq!(cx.st.ring_state, "home", "one-shots never move state");
        assert_eq!(cx.st.oled_mode, "ripple");
        assert_eq!(cx.st.oled_text, None, "every mood but alarmed clears text");
        assert!(matches!(feel(&mut cx, None, true, None).unwrap(),
                         Logged::Mood { ref ring, .. } if ring == "glitch"));
    }

    #[test]
    fn alarmed_shouts_and_asleep_dims() {
        let mut cx = Cortex::new();
        feel(&mut cx, Some("alarmed"), false, None).unwrap();
        assert_eq!(cx.st.oled_text.as_deref(), Some("!! FLATLINE !!"));
        assert_eq!(cx.st.ring_state, "alarm");
        feel(&mut cx, Some("asleep"), false, None).unwrap();
        assert!(cx.st.oled_dim);
        assert_eq!(cx.st.oled_text, None);
    }

    #[test]
    fn an_unknown_feeling_names_the_moods_it_may_have_meant() {
        let mut cx = Cortex::new();
        let e = feel(&mut cx, Some("transcendent"), false, None).unwrap_err();
        // sorted in the 400 text, insertion-ordered in the walk: both are the
        // reference's own quirk, kept because the door's tests quote it.
        assert_eq!(e, "feeling must be one of alarmed/annoyed/asleep/busy/calm/pleased");
    }

    #[test]
    fn display_text_survives_only_in_text_mode() {
        let mut cx = Cortex::new();
        display(&mut cx, Some("text"), Some("hello"), None, None).unwrap();
        assert_eq!(cx.st.oled_text.as_deref(), Some("hello"));
        display(&mut cx, Some("scope"), Some("dropped"), None, None).unwrap();
        assert_eq!(cx.st.oled_text, None, "a mode change clears stray text");
        assert_eq!(cx.st.oled_mode, "scope");
        // no fields at all: nothing moves, nothing logs
        assert_eq!(display(&mut cx, None, None, None, None).unwrap(), Logged::None);
        assert!(display(&mut cx, Some("disco"), None, None, None).is_err());
    }

    #[test]
    fn console_on_is_a_mode_and_off_stomps_to_scope() {
        let mut cx = Cortex::new();
        display(&mut cx, Some("noise"), None, None, None).unwrap();
        ripperdoc(&mut cx, Some(true), None).unwrap();
        assert!(cx.st.ripperdoc);
        assert_eq!(cx.st.oled_mode, "ripperdoc");
        ripperdoc(&mut cx, Some(false), None).unwrap();
        assert_eq!(cx.st.oled_mode, "scope", "off forces scope, not 'previous'");
    }

    #[test]
    fn page_alone_changes_no_mode_and_logs_nothing() {
        let mut cx = Cortex::new();
        assert_eq!(
            ripperdoc(&mut cx, None, Some("power")).unwrap(),
            Logged::None,
            "page-only calls are not events"
        );
        assert_eq!(cx.st.page, "power");
        assert_eq!(cx.st.oled_mode, "scope");
        let e = ripperdoc(&mut cx, None, Some("mood")).unwrap_err();
        assert!(e.contains("fault"), "the seventh page must be in the 400 text");
    }
}
