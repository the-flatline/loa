//! The matrix's vocabulary: one 8x8 shape per meaning, no text, and no motion
//! that isn't carrying information.
//!
//! The panel's job is the one the other displays can't do: an unmistakable
//! shape, readable across a room, at a glance, with nothing to read. So the
//! vocabulary is small and fixed, and every glyph has to survive being eight
//! pixels wide.
//!
//! Glyphs are written the way they're seen: `rows[0]` is the top row, and the
//! most significant bit is the leftmost column. `0b00111100` is a four-pixel
//! bar in the middle of a row. If a shape is hard to read in this source, it
//! will be harder to read on the panel.
//!
//! Shape says WHAT. Motion says HOW MUCH. Never the other way round:
//!   steady      a state that is holding
//!   slow flash  advisory — worth knowing
//!   fast flash  happening now — look at me
//!
//! Sealed and breach are deliberately the same object in two states — a lock
//! shut, a lock open — so a glance compares rather than re-reads. The first
//! version of breach was a broken ring, which read as the letter G at a
//! distance. A symbol has to survive being glanced at, not studied.

/// One shape, and the word it stands for.
pub struct Glyph {
    pub name: &'static str,
    pub rows: [u8; 8],
}

/// Every LED off. Also a state — the resting face of a body that is not
/// asking for anything.
#[rustfmt::skip]
pub const DARK: Glyph = Glyph {
    name: "dark",
    rows: [
        0b00000000,
        0b00000000,
        0b00000000,
        0b00000000,
        0b00000000,
        0b00000000,
        0b00000000,
        0b00000000,
    ],
};

/// A padlock, shut. The shackle sits on the body; nothing is open.
#[rustfmt::skip]
pub const SEALED: Glyph = Glyph {
    name: "sealed",
    rows: [
        0b00111100,
        0b00100100,
        0b00100100,
        0b01111110,
        0b01100110,
        0b01100110,
        0b01111110,
        0b01111110,
    ],
};

/// The same padlock with its shackle lifted off the body. Something was
/// opened, and the keyhole is the only part that is still closed.
#[rustfmt::skip]
pub const BREACH: Glyph = Glyph {
    name: "breach",
    rows: [
        0b01111000,
        0b01001000,
        0b01000000,
        0b01111110,
        0b01100110,
        0b01100110,
        0b01111110,
        0b01111110,
    ],
};

/// An X. Something that should be running is not.
#[rustfmt::skip]
pub const FAULT: Glyph = Glyph {
    name: "fault",
    rows: [
        0b11000011,
        0b01100110,
        0b00111100,
        0b00011000,
        0b00011000,
        0b00111100,
        0b01100110,
        0b11000011,
    ],
};

/// One bar. Worth knowing, not yet worth stopping for.
#[rustfmt::skip]
pub const ATTENTION: Glyph = Glyph {
    name: "attention",
    rows: [
        0b00011000,
        0b00011000,
        0b00011000,
        0b00011000,
        0b00011000,
        0b00011000,
        0b00000000,
        0b00011000,
    ],
};

/// Two bars. More of the same word, louder — the shape carries the size of the
/// problem, so the eye doesn't have to time a flash to rank it.
#[rustfmt::skip]
pub const URGENT: Glyph = Glyph {
    name: "urgent",
    rows: [
        0b01100110,
        0b01100110,
        0b01100110,
        0b01100110,
        0b01100110,
        0b01100110,
        0b00000000,
        0b01100110,
    ],
};

/// Not a state. A moment — the body saying it's still in here, shown rarely
/// and briefly, like the ring's rare glitch.
#[rustfmt::skip]
pub const INVADER: Glyph = Glyph {
    name: "invader",
    rows: [
        0b00100100,
        0b00011000,
        0b01111110,
        0b11011011,
        0b11111111,
        0b10111101,
        0b10100101,
        0b00100100,
    ],
};

/// The states, in presentation order. INVADER is deliberately absent: it is
/// not part of the alarm vocabulary and must never be reachable as a status.
pub const ALL: [&Glyph; 6] = [&DARK, &SEALED, &BREACH, &FAULT, &ATTENTION, &URGENT];

/// Look one up by name, states only.
pub fn by_name(name: &str) -> Option<&'static Glyph> {
    ALL.iter()
        .copied()
        .find(|g| g.name == name)
        .or_else(|| (name == INVADER.name).then_some(&INVADER))
}
