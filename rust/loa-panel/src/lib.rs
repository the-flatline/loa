//! loa-panel — the OLED daemon's device layer.
//!
//! `panel.rs` owns the glass (SPI0, the init sequence, DC and RES); `main.rs`
//! owns the feed and the loop. Split so the hardware is testable without a
//! socket and the loop is testable without a panel.
pub mod panel;
