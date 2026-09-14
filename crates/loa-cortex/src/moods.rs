//! The mood table: one word in, the face and ring picture out.
//!
//! MOODS is ORDERED — `next` advances through it in this order. A Vec, not a
//! map: iteration order IS the behaviour.

pub struct Mood {
    pub name: &'static str,
    pub ring: &'static str,
    /// true = a record on the event topic, not a state change. The ring plays
    /// it once when it is next at home.
    pub one_shot: bool,
    pub oled_mode: &'static str,
    pub oled_text: Option<&'static str>,
    pub oled_dim: bool,
}

pub const MOODS: &[Mood] = &[
    Mood { name: "calm",    ring: "home",  one_shot: false, oled_mode: "scope",  oled_text: None, oled_dim: false },
    Mood { name: "busy",    ring: "busy",  one_shot: false, oled_mode: "ecg",    oled_text: None, oled_dim: false },
    Mood { name: "pleased", ring: "scan",  one_shot: true,  oled_mode: "ripple", oled_text: None, oled_dim: false },
    Mood { name: "annoyed", ring: "glitch",one_shot: true,  oled_mode: "noise",  oled_text: None, oled_dim: false },
    Mood { name: "alarmed", ring: "alarm", one_shot: false, oled_mode: "text",   oled_text: Some("!! FLATLINE !!"), oled_dim: false },
    Mood { name: "asleep",  ring: "home",  one_shot: false, oled_mode: "ripple", oled_text: None, oled_dim: true },
];

pub fn by_name(name: &str) -> Option<&'static Mood> {
    MOODS.iter().find(|m| m.name == name)
}

/// The `next` walk: from the current mood to the one after it; unknown or
/// missing current starts the cycle at calm (index 0).
pub fn next_from(current: &str) -> &'static Mood {
    let i = MOODS.iter().position(|m| m.name == current);
    match i {
        Some(i) => &MOODS[(i + 1) % MOODS.len()],
        None => &MOODS[0],
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_next_walk_cycles_in_order() {
        // calm -> busy -> pleased -> annoyed -> alarmed -> asleep -> calm.
        // The ORDER is the behaviour; a shuffle here changes the console.
        let mut cur = "calm";
        let mut seen = vec![];
        for _ in 0..MOODS.len() {
            let m = next_from(cur);
            seen.push(m.name);
            cur = m.name;
        }
        assert_eq!(
            seen,
            vec!["busy", "pleased", "annoyed", "alarmed", "asleep", "calm"]
        );
    }

    #[test]
    fn an_unknown_current_starts_at_calm() {
        assert_eq!(next_from("transcendent").name, "calm");
        assert_eq!(next_from("").name, "calm");
    }

    #[test]
    fn only_pleased_and_annoyed_play_records() {
        // One-shots are records, never state: the ring plays them at home and
        // no flag is left behind to be cleared by the wrong daemon.
        let shots: Vec<&str> = MOODS.iter().filter(|m| m.one_shot).map(|m| m.name).collect();
        assert_eq!(shots, vec!["pleased", "annoyed"]);
        let alarmed = by_name("alarmed").unwrap();
        assert_eq!(alarmed.oled_text, Some("!! FLATLINE !!"));
        assert!(by_name("asleep").unwrap().oled_dim);
        assert!(!by_name("pleased").unwrap().oled_dim);
    }
}
