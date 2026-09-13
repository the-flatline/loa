//! loa-ring — the ring daemon's device layer.
//!
//! `neopixel.rs` owns the strip (SPI1, the WS2812 bit timing); `main.rs` owns
//! the feed and the loop. Split so the encoding is testable without a strip
//! and the loop is testable without a bus.
pub mod neopixel;
