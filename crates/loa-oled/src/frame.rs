//! The framebuffer: 128x64, 1bpp, page-major, the layout the SH1106 eats.
//!
//! Ported from the reference `face.Frame` exactly. The index arithmetic is
//! `(y >> 3) * 128 + x`, bit `1 << (y & 7)` — which makes buffer index
//! `page * WIDTH + x` contiguous along a page, so `buf` IS the frame that
//! goes to the glass and to the feed: no conversion step, nothing to drift.
//!
//! Off-screen writes are DROPPED, not clamped — the reference's px() returns
//! when the coordinate is outside 0..=WIDTH / 0..=HEIGHT. Clamping would
//! smear a clipped line along the edge; a drop just doesn't draw it.

use crate::fonts;

pub const WIDTH: usize = 128;
pub const HEIGHT: usize = 64;
pub const PAGES: usize = 8;

/// A frame as the panel wants it: 8 pages of 128 bytes.
pub type Buf = [u8; PAGES * WIDTH];

pub struct Frame {
    pub buf: Vec<u8>,
}

impl Default for Frame {
    fn default() -> Self {
        Self::new()
    }
}

impl Frame {
    pub fn new() -> Self {
        Frame {
            buf: vec![0u8; PAGES * WIDTH],
        }
    }

    pub fn clear(&mut self) {
        for b in self.buf.iter_mut() {
            *b = 0;
        }
    }

    /// One pixel. `on=false` clears the bit and leaves the neighbours alone.
    pub fn px(&mut self, x: i32, y: i32, on: bool) {
        if x < 0 || x >= WIDTH as i32 || y < 0 || y >= HEIGHT as i32 {
            return;
        }
        let (x, y) = (x as usize, y as usize);
        let i = (y >> 3) * WIDTH + x;
        let bit = 1u8 << (y & 7);
        if on {
            self.buf[i] |= bit;
        } else {
            self.buf[i] &= !bit;
        }
    }

    /// Bresenham, verbatim from the reference: the endpoint is included and
    /// off-screen steps are rejected one pixel at a time inside px().
    pub fn line(&mut self, mut x0: i32, mut y0: i32, x1: i32, y1: i32, on: bool) {
        let dx = (x1 - x0).abs();
        let sx = if x0 < x1 { 1 } else { -1 };
        let dy = -(y1 - y0).abs();
        let sy = if y0 < y1 { 1 } else { -1 };
        let mut err = dx + dy;
        loop {
            self.px(x0, y0, on);
            if x0 == x1 && y0 == y1 {
                break;
            }
            let e2 = 2 * err;
            if e2 >= dy {
                err += dy;
                x0 += sx;
            }
            if e2 <= dx {
                err += dx;
                y0 += sy;
            }
        }
    }

    /// 5x7 bitmap text, uppercase; unknown chars render as spaces.
    pub fn text(&mut self, mut x: i32, y: i32, s: &str, on: bool) {
        for ch in s.chars() {
            let glyph = fonts::g5x7(ch);
            for (cx, col) in [glyph.0, glyph.1, glyph.2, glyph.3, glyph.4]
                .into_iter()
                .enumerate()
            {
                for cy in 0..7 {
                    if col & (1 << cy) != 0 {
                        self.px(x + cx as i32, y + cy, on);
                    }
                }
            }
            x += 6;
        }
    }

    pub fn text_width(s: &str) -> usize {
        s.chars().count() * 6
    }

    /// 3x5 bitmap text, uppercase, 4px advance — status-board density.
    pub fn text3x5(&mut self, mut x: i32, y: i32, s: &str, on: bool) {
        for ch in s.chars() {
            let glyph = fonts::g3x5(ch);
            for (cx, col) in [glyph.0, glyph.1, glyph.2].into_iter().enumerate() {
                for cy in 0..5 {
                    if col & (1 << cy) != 0 {
                        self.px(x + cx as i32, y + cy, on);
                    }
                }
            }
            x += 4;
        }
    }

    pub fn text3x5_width(s: &str) -> usize {
        s.chars().count() * 4
    }

    /// Copy the framebuffer out as the wire/glass frame. Panics on a buffer
    /// that is not FACE_BYTES long — that is a code bug, not a runtime fact.
    pub fn frame_bytes(&self) -> Buf {
        let mut out = [0u8; PAGES * WIDTH];
        out.copy_from_slice(&self.buf);
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn bit(buf: &[u8], x: usize, y: usize) -> bool {
        buf[(y >> 3) * WIDTH + x] & (1 << (y & 7)) != 0
    }

    #[test]
    fn px_lands_on_the_page_major_index() {
        let mut f = Frame::new();
        f.px(0, 0, true);
        assert_eq!(f.buf[0], 0x01);
        f.px(1, 3, true);
        assert_eq!(f.buf[1], 0x08);
        f.px(0, 8, true); // page 1, bit 0
        assert_eq!(f.buf[WIDTH], 0x01);
        f.px(127, 63, true); // the far corner
        assert_eq!(f.buf[7 * WIDTH + 127], 0x80);
    }

    #[test]
    fn px_off_screen_is_dropped_not_wrapped() {
        let mut f = Frame::new();
        f.px(-1, 0, true);
        f.px(0, -1, true);
        f.px(128, 0, true);
        f.px(0, 64, true);
        assert!(f.buf.iter().all(|&b| b == 0));
    }

    #[test]
    fn px_clear_leaves_the_neighbours() {
        let mut f = Frame::new();
        f.px(5, 5, true);
        f.px(6, 5, true);
        f.px(5, 5, false);
        assert!(!bit(&f.buf, 5, 5));
        assert!(bit(&f.buf, 6, 5));
    }

    #[test]
    fn horizontal_line_is_exact_bytes_including_endpoints() {
        let mut f = Frame::new();
        f.line(0, 0, 7, 0, true);
        assert_eq!(&f.buf[..8], &[0x01; 8]);
        assert_eq!(f.buf[8], 0);
    }

    #[test]
    fn steep_line_hits_every_row() {
        let mut f = Frame::new();
        f.line(10, 0, 10, 63, true);
        for y in 0..64 {
            assert!(bit(&f.buf, 10, y), "row {y} missed");
        }
    }

    #[test]
    fn text_uses_the_font_table_and_advances_six() {
        let mut f = Frame::new();
        f.text(0, 0, "A", true);
        // g5x7('A') = (0x0E, 0x11, 0x1F, 0x11, 0x11), column-major bit0=top
        let glyph = fonts::g5x7('A');
        for (cx, col) in [glyph.0, glyph.1, glyph.2, glyph.3, glyph.4].into_iter().enumerate() {
            for cy in 0..7 {
                assert_eq!(bit(&f.buf, cx, cy), col & (1 << cy) != 0);
            }
        }
        f.text(0, 20, "AA", true);
        // the second A starts at x=6, so column 0 of the second glyph is x=6
        assert_eq!(bit(&f.buf, 6, 20), (glyph.0 & 1) != 0);
        assert_eq!(Frame::text_width("AB"), 12);
    }

    #[test]
    fn text3x5_advances_four() {
        let mut f = Frame::new();
        f.text3x5(0, 0, "AB", true);
        let b = fonts::g3x5('B');
        for (cx, col) in [b.0, b.1, b.2].into_iter().enumerate() {
            for cy in 0..5 {
                assert_eq!(bit(&f.buf, 4 + cx, cy), col & (1 << cy) != 0);
            }
        }
        assert_eq!(Frame::text3x5_width("A"), 4);
    }

    #[test]
    fn frame_bytes_is_1024() {
        let f = Frame::new();
        assert_eq!(f.frame_bytes().len(), 1024);
    }
}
