//! The chassis's shapes. Two screens, fixed by hardware, shared so a renderer
//! and the wire can never disagree about a frame's size.

/// The OLED: 128x64, 1bpp, 8 pages of 128 bytes. The panel as driven — this
/// IS the frame: raw bytes, no description, no palette.
pub mod oled {
    pub const WIDTH: usize = 128;
    pub const HEIGHT: usize = 64;
    pub const PAGES: usize = 8;
    pub const BYTES: usize = WIDTH * PAGES; // 1024
}

/// The ring: 24 RGB LEDs in a circle — effectively a 1x24 screen. Raw 72
/// bytes, GRB order is the wire's business, not the shape's.
pub mod ring {
    pub const LEDS: usize = 24;
    pub const BYTES: usize = LEDS * 3; // 72
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn frame_sizes_match_the_wire_contract() {
        // The proto comments and the daemons both quote these numbers; the
        // constants live here so there is one source.
        assert_eq!(oled::BYTES, 1024);
        assert_eq!(ring::BYTES, 72);
    }
}
