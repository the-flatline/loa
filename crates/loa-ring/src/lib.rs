//! loa-ring — the ring daemon's rendering layers.
//!
//! `tells.rs` is the voice: the precedence ladder and the animation maths,
//! carried from the Python reference. `render.rs` is the clock and the
//! dither: state -> 72 code-space bytes. `encode.rs` is the strip: the
//! WS2812 pulse codes over SPI. Split so each layer is testable without the
//! others — no wire, no bus, no strip. The daemon itself is `main.rs`.

pub mod encode;
pub mod render;
pub mod tells;
