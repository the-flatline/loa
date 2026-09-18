//! loa-oled: the face. Receives state, renders its own pixels, drives
//! the glass, pushes the frame back.
//!
//! The renderer port lives here: `frame` is the 128x64 framebuffer,
//! `face` the mode animations, `render` the FaceRenderer equivalent (the
//! wash, the key-rebuild rule, the fault takeover), `rstate` the held-
//! topic view the pages draw from. `oled` and `fonts` are the glass and
//! the glyph tables, already proven.

pub mod face;
pub mod fonts;
pub mod fonts_draw;
pub mod frame;
pub mod oled;
pub mod render;
pub mod rstate;
