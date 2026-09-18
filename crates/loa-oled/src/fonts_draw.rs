//! Drawing with the row-major fonts (Amiga, Topaz) into a Frame.
//!
//! The glyph BITS live in fonts.rs as generated data; this is the placement
//! code that was hand-written in the reference's amiga.py and topaz.py. Same
//! rules, kept exact:
//!
//!   * advance = glyph width + 1 (the 1px letter spacing is inside every
//!     measurement, width() included);
//!   * the reference's amiga.draw loops columns `range(len(bin(mask)) - 2)`
//!     — the bit-length of the mask, not a fixed cell. Bits above a row's
//!     bit-length cannot be set, so this iterates the full cell instead: same
//!     pixels, one shape.
//!   * unknown chars render as the space glyph, lowercase asks for uppercase
//!     (both are baked into the fonts:: lookups).

use crate::fonts;
use crate::frame::Frame;

/// Amiga Forever at 8px: `rows` is 7 bitmasks, row-major, bit b = column b.
pub fn amiga8_width(s: &str) -> usize {
    s.chars().map(|c| fonts::gamiga8(c).0 as usize + 1).sum()
}

pub fn amiga8(f: &mut Frame, s: &str, x0: i32, y0: i32, on: bool) {
    let mut cx = x0;
    for ch in s.chars() {
        let (w, rows) = fonts::gamiga8(ch);
        for (r, mask) in rows.iter().enumerate() {
            for b in 0..w {
                if mask & (1 << b) != 0 {
                    f.px(cx + b as i32, y0 + r as i32, on);
                }
            }
        }
        cx += w as usize as i32 + 1;
    }
}

/// Amiga Forever at 10px (the indicator-label size): 9 rows, u16 masks.
pub fn amiga10(f: &mut Frame, s: &str, x0: i32, y0: i32, on: bool) {
    let mut cx = x0;
    for ch in s.chars() {
        let (w, rows) = fonts::gamiga10(ch);
        for (r, mask) in rows.iter().enumerate() {
            for b in 0..w {
                if mask & (1u16 << b) != 0 {
                    f.px(cx + b as i32, y0 + r as i32, on);
                }
            }
        }
        cx += w as usize as i32 + 1;
    }
}

/// Topaz 16-row (the showoff demo): 16 rows of 8-bit masks, the same
/// one-pixel-gap advance. The table is uppercase-only; gtopaz folds.
pub fn topaz(f: &mut Frame, s: &str, x0: i32, y0: i32) {
    let mut cx = x0;
    for ch in s.chars() {
        let (w, rows) = fonts::gtopaz(ch);
        for (r, mask) in rows.iter().enumerate() {
            for b in 0..w {
                if mask & (1 << b) != 0 {
                    f.px(cx + b as i32, y0 + r as i32, true);
                }
            }
        }
        cx += w as usize as i32 + 1;
    }
}

/// The Topaz marquee strip: 16 rows of packed bits, the letter-space 0 kept
/// as a hole (the reference's strip() builds it the same way, per glyph).
/// Row r's column sx is `rows[r][sx / 8] >> (sx % 8) & 1`.
pub fn topaz_strip(s: &str) -> (Vec<Vec<u8>>, usize) {
    let mut rows = vec![Vec::new(); 16];
    for ch in s.chars() {
        let (w, glyph) = fonts::gtopaz(ch);
        for r in 0..16 {
            for b in 0..w {
                rows[r].push(if glyph[r] & (1 << b) != 0 { 1 } else { 0 });
            }
            rows[r].push(0); // the 1px spacing
        }
    }
    let w = rows.first().map_or(0, |v| v.len());
    // pack each row into bytes for cheap reads in the animation
    let packed = rows
        .iter()
        .map(|row| {
            let mut bytes = vec![0u8; row.len().div_ceil(8)];
            for (i, &bit) in row.iter().enumerate() {
                if bit != 0 {
                    bytes[i / 8] |= 1 << (i % 8);
                }
            }
            bytes
        })
        .collect();
    (packed, w)
}

pub fn strip_bit(rows: &[Vec<u8>], r: usize, sx: usize) -> bool {
    rows[r][sx / 8] & (1 << (sx % 8)) != 0
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::frame::WIDTH;

    fn bit(buf: &[u8], x: usize, y: usize) -> bool {
        buf[(y >> 3) * WIDTH + x] & (1 << (y & 7)) != 0
    }

    #[test]
    fn amiga8_places_the_first_glyph_then_the_space() {
        let mut f = Frame::new();
        amiga8(&mut f, "II", 0, 0, true);
        // 'I' rows: [15, 6, 6, 6, 6, 6, 15] — row 1 of the first I at x=1..2
        assert!(bit(&f.buf, 1, 1));
        assert!(bit(&f.buf, 2, 1));
        assert!(!bit(&f.buf, 3, 1));
        // advance 8+1: the second I starts at x=9; row 0 = 15 -> cols 0..3 lit
        assert!(bit(&f.buf, 9, 0));
        assert!(bit(&f.buf, 12, 0));
        assert!(!bit(&f.buf, 13, 0));
        assert_eq!(amiga8_width("AB"), 18); // (8+1) * 2
    }

    #[test]
    fn amiga8_off_paints_the_glyph_away() {
        let mut f = Frame::new();
        for y in 0..8 {
            for x in 0..9 {
                f.px(x, y, true);
            }
        }
        amiga8(&mut f, "I", 0, 0, false);
        // row 1's middle bits are knocked out
        assert!(!bit(&f.buf, 1, 1));
        assert!(bit(&f.buf, 0, 1)); // 6 = 0b0110, col 0 dark in the glyph
        assert!(bit(&f.buf, 3, 1));
    }

    #[test]
    fn topaz_row_masks_land_row_major() {
        let mut f = Frame::new();
        topaz(&mut f, "A", 0, 0);
        // gtopaz('A') w=6, rows[0]=0x1E -> cols 1..4
        assert!(!bit(&f.buf, 0, 0));
        for x in 1..5 {
            assert!(bit(&f.buf, x, 0));
        }
        assert!(!bit(&f.buf, 5, 0));
    }

    #[test]
    fn strip_keeps_the_spacing_holes() {
        let (rows, w) = topaz_strip("!!");
        // '!' is 2 wide -> each glyph contributes 2 bits + 1 space = 3
        assert_eq!(w, 6);
        assert!(!strip_bit(&rows, 0, 2)); // the gap after the first glyph
        assert!(strip_bit(&rows, 0, 0)); // '!' row 0 = 3, cols 0-1
        assert!(strip_bit(&rows, 0, 3));
    }
}
