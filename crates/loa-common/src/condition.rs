//! The condition vocabulary: exactly four states, in the words the body uses
//! on the wire. Consumers match on these strings; a fifth value means
//! somebody invented a word instead of checking.
//!
//! The precedence is NOT here — it deliberately differs between the fault
//! sweep and the renderer, and unifying them would be a lie about the body.
//! Each consumer computes its own from these words.

pub const WELL: &str = "well";
pub const NIGGLE: &str = "niggle";
pub const HURTS: &str = "hurts";
pub const MUTE: &str = "mute";

pub const CONDITIONS: [&str; 4] = [WELL, NIGGLE, HURTS, MUTE];
