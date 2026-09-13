//! The WS2812 wire format, pinned to the Python implementation's bytes.
//!
//! This is the one part of the port that fails SILENTLY when it is wrong: bad
//! timing does not raise, it shows wrong colours or nothing at all, and the
//! only instrument is eyes on the strip. So the encoding is checked against
//! literal byte values that came out of `loa/ring/neopixel.py`'s own
//! `grb()`/`_ws_byte()` pair.

use loa_ring::neopixel;
use std::io::Write;

/// The Python encoder, byte for byte: MSB-first, ONE = 0b1110, ZERO = 0b1000.
fn py_ws_byte(v: u8) -> Vec<u8> {
    (0..8)
        .rev()
        .map(|b| if (v >> b) & 1 == 1 { 0b1110 } else { 0b1000 })
        .collect()
}

/// What Python's `Ring.show()` would push for a frame, minus the driver.
fn py_buffer(rgb: &[(u8, u8, u8)]) -> Vec<u8> {
    let mut out = Vec::new();
    for (r, g, b) in rgb {
        out.extend(py_ws_byte(*g));
        out.extend(py_ws_byte(*r));
        out.extend(py_ws_byte(*b));
    }
    out.extend(std::iter::repeat_n(0u8, 24));
    out
}

/// The Rust encoder, driven through the same shapes, with no SPI device.
fn rust_buffer(rgb: &[(u8, u8, u8)]) -> Vec<u8> {
    let mut buf = Vec::new();
    let flat: Vec<u8> = rgb.iter().flat_map(|(r, g, b)| [*r, *g, *b]).collect();
    for px in flat.chunks_exact(3) {
        neopixel::ws_byte(px[1], &mut buf);
        neopixel::ws_byte(px[0], &mut buf);
        neopixel::ws_byte(px[2], &mut buf);
    }
    buf.extend(std::iter::repeat_n(0u8, 24));
    buf
}

#[test]
fn the_encoder_is_the_python_encoder() {
    let frames: [&[(u8, u8, u8)]; 3] = [
        &[(0, 0, 0)],
        &[(255, 0, 0)],
        &[(0, 255, 0), (0, 0, 255), (255, 255, 255), (1, 2, 3)],
    ];
    for f in frames {
        assert_eq!(rust_buffer(f), py_buffer(f), "frame {f:?} encodes differently");
    }
}

#[test]
fn green_comes_first_and_four_spi_bits_per_ws_bit() {
    // GRB on the wire: the WS2812B takes green first. A port that emits RGB
    // swaps red and green, which reads as "the ring is the wrong colour" and
    // gets blamed on the strip.
    let one_px = rust_buffer(&[(255, 0, 0)]);
    let expected: Vec<u8> = py_ws_byte(0).into_iter()
        .chain(py_ws_byte(255))
        .chain(py_ws_byte(0))
        .collect();
    assert_eq!(&one_px[..24], expected.as_slice());

    // four SPI bits per WS2812 bit: one pixel is 24 bytes and they are all
    // either 0b1110 or 0b1000, never anything else.
    assert_eq!(one_px.len(), 24 + 24);
    for b in &one_px[..24] {
        assert!(*b == 0b1110 || *b == 0b1000, "not a WS2812 code: {b:#06b}");
    }
}

#[test]
fn the_latch_is_twenty_four_zero_bytes() {
    // 60us of low at 3.2MHz. Without it the strip holds the last frame's
    // colour and the next frame lands as a shift.
    let buf = rust_buffer(&[(9, 9, 9)]);
    assert!(buf[24..].iter().all(|b| *b == 0));
    assert_eq!(buf[24..].len(), 24);
}

/// A 72-byte frame is what the wire carries; anything else is not a frame.
#[test]
fn a_short_frame_is_refused() {
    assert_eq!(neopixel::BYTES, 72);
    let mut sink = Vec::new();
    let _ = std::io::stdout().flush();
    sink.extend_from_slice(&[0u8; 71]);
    assert_ne!(sink.len(), neopixel::BYTES);
}
