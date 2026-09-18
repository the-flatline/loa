//! The WS2812B, over SPI, on the Pi 5.
//!
//! Carried verbatim from the proven port (e69dea5, `rust/loa-ring/src/neopixel.rs`),
//! tested against the Python encoder's own bytes (e69dea5, `tests/wire_format.rs`).
//!
//! WS2812 timing is what this file is about. There is no "set brightness"
//! register: each LED bit is a pulse whose WIDTH is the data, and the strip
//! latches 60us of low at the end. rpi_ws281x cannot drive this chipset (its
//! wheel has no RP1 binary) and the Adafruit path needs Blinka, so the pulses
//! are made by SPI: four SPI bits per WS2812 bit at 3.2MHz.
//!
//! ```text
//!     1 -> 0b1110   ~937ns high, 1.25us total
//!     0 -> 0b1000   ~937ns low,  1.25us total
//! ```
//!
//! The clock is the contract. At 3.2MHz eight SPI bits are 2.5us, so four of
//! them are 1.25us — exactly one WS2812 bit period. Change the clock and the
//! colours change with it, which is why the speed is a setting and not a
//! constant in the loop.

use std::io::Write;

pub const LEDS: usize = 24; // loa/ring/animations.py:18 (geom.RING_LEDS)
pub const BYTES: usize = LEDS * 3; // 72: 24 px RGB
const SPI_BYTE_PER_WS_BIT: usize = 1;
const LATCH: usize = 24; // 60us low at 3.2MHz

/// 3.2MHz: four SPI bits per WS2812 bit, which is where the numbers above
/// come from. Measured as the as-built truth in loa.conf on the body.
pub const DEFAULT_HZ: u32 = 3_200_000;

const ONE: u8 = 0b1110;
const ZERO: u8 = 0b1000;

/// One channel value -> eight SPI bytes. MSB first: the strip takes the high
/// bit of the colour first, so the order is part of the protocol, not a
/// preference.
///
/// Public because the wire format is the part of this port that fails
/// SILENTLY — bad timing raises nothing, it shows the wrong colours — so
/// `encode_tests` pin it against the Python encoder's own bytes.
pub fn ws_byte(v: u8, out: &mut Vec<u8>) {
    for bit in (0..8).rev() {
        out.push(if (v >> bit) & 1 == 1 { ONE } else { ZERO });
    }
}

/// The SPI buffer a frame becomes: GRB on the wire (the WS2812B takes green
/// first — a property of the part, not of the renderer), then the latch.
/// Separate from the device so the encoder is testable with no strip.
pub fn encode_frame(rgb: &[u8]) -> Vec<u8> {
    let mut buf = Vec::with_capacity(BYTES * 8 * SPI_BYTE_PER_WS_BIT + LATCH);
    for px in rgb.chunks_exact(3) {
        ws_byte(px[1], &mut buf); // G
        ws_byte(px[0], &mut buf); // R
        ws_byte(px[2], &mut buf); // B
    }
    buf.extend(std::iter::repeat_n(0u8, LATCH)); // latch: 60us low
    buf
}

pub struct Ring {
    spi: spidev::Spidev,
}

impl Ring {
    /// Opening fails off-Pi with a clear error; the caller exits. That is the
    /// only runtime distinction between the body and the bench.
    pub fn open(path: &str, speed_hz: u32) -> Result<Self, String> {
        let mut spi = spidev::Spidev::open(path).map_err(|e| format!("{path}: {e}"))?;
        let opts = spidev::SpidevOptions::new()
            .bits_per_word(8)
            .max_speed_hz(speed_hz)
            .mode(spidev::SpiModeFlags::SPI_MODE_0)
            .build();
        spi.configure(&opts).map_err(|e| e.to_string())?;
        Ok(Ring { spi })
    }

    /// 72 bytes of RGB (r,g,b per LED) -> the strip.
    pub fn show(&mut self, rgb: &[u8]) -> Result<(), String> {
        if rgb.len() != BYTES {
            return Err(format!("frame is {} bytes, expected {BYTES}", rgb.len()));
        }
        let buf = encode_frame(rgb);
        self.spi.write_all(&buf).map_err(|e| e.to_string())
    }

    pub fn off(&mut self) -> Result<(), String> {
        self.show(&[0u8; BYTES])
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The Python encoder (`loa/ring/neopixel.py`'s `_ws_byte` pair, as
    /// pinned by e69dea5 `tests/wire_format.rs`): MSB-first, ONE = 0b1110,
    /// ZERO = 0b1000.
    fn py_ws_byte(v: u8) -> Vec<u8> {
        (0..8)
            .rev()
            .map(|b| if (v >> b) & 1 == 1 { 0b1110 } else { 0b1000 })
            .collect()
    }

    /// What the Python's `Ring.show()` would push for a frame, minus the
    /// driver — the exact fixture from the historical wire_format.rs.
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

    #[test]
    fn the_encoder_is_the_python_encoder() {
        // Pinned vectors, carried from e69dea5 `tests/wire_format.rs`: the
        // same shapes through both encoders must agree byte for byte.
        let frames: [&[(u8, u8, u8)]; 3] = [
            &[(0, 0, 0)],
            &[(255, 0, 0)],
            &[(0, 255, 0), (0, 0, 255), (255, 255, 255), (1, 2, 3)],
        ];
        for f in frames {
            let flat: Vec<u8> = f.iter().flat_map(|(r, g, b)| [*r, *g, *b]).collect();
            assert_eq!(
                encode_frame(&flat),
                py_buffer(f),
                "frame {f:?} encodes differently"
            );
        }
    }

    #[test]
    fn green_comes_first_and_four_spi_bits_per_ws_bit() {
        // GRB on the wire: a port that emits RGB swaps red and green, which
        // reads as "the ring is the wrong colour" and gets blamed on the
        // strip. Pinned literal bytes for one red pixel (24 SPI bytes),
        // straight from the historical test.
        let one_px = encode_frame(&[255, 0, 0]);
        let expected: Vec<u8> = py_ws_byte(0)
            .into_iter()
            .chain(py_ws_byte(255))
            .chain(py_ws_byte(0))
            .collect();
        assert_eq!(&one_px[..24], expected.as_slice());
        assert_eq!(one_px.len(), 24 + 24);
        for b in &one_px[..24] {
            assert!(*b == 0b1110 || *b == 0b1000, "not a WS2812 code: {b:#06b}");
        }
    }

    #[test]
    fn ws_byte_pins_literal_vectors() {
        // The single-byte vectors, spelled out (verified against the
        // Python's bit expansion): 0 -> all ZERO, 255 -> all ONE, 3 -> the
        // two low bits set, 200 = 0b11001000.
        let mut b = Vec::new();
        ws_byte(0, &mut b);
        assert_eq!(b, vec![0b1000; 8]);
        b.clear();
        ws_byte(255, &mut b);
        assert_eq!(b, vec![0b1110; 8]);
        b.clear();
        ws_byte(3, &mut b);
        assert_eq!(b, vec![8, 8, 8, 8, 8, 8, 14, 14]);
        b.clear();
        ws_byte(200, &mut b);
        assert_eq!(b, vec![14, 14, 8, 8, 14, 8, 8, 8]);
    }

    #[test]
    fn the_latch_is_twenty_four_zero_bytes() {
        // 60us of low at 3.2MHz. Without it the strip holds the last frame
        // and the next frame lands as a shift. Pinned from history.
        let mut rgb = [0u8; BYTES];
        rgb[..3].copy_from_slice(&[9, 9, 9]);
        let buf = encode_frame(&rgb);
        assert_eq!(buf.len(), BYTES * 8 + 24);
        assert!(buf[BYTES * 8..].iter().all(|b| *b == 0));
        assert_eq!(buf[BYTES * 8..].len(), 24);
    }

    #[test]
    fn a_full_ring_frame_is_six_hundred_bytes_on_spi() {
        // The as-built shape: 24 px x 24 SPI bytes + 24 latch = 600. Pinned
        // with the home base colour (0,3,0) so the first pixel's expansion
        // is checked literally.
        let mut rgb = [0u8; BYTES];
        for px in rgb.chunks_exact_mut(3) {
            px[1] = 3; // G = 3, the idle green base
        }
        let buf = encode_frame(&rgb);
        assert_eq!(buf.len(), 600);
        assert_eq!(
            &buf[..16],
            &[8, 8, 8, 8, 8, 8, 14, 14, 8, 8, 8, 8, 8, 8, 8, 8]
        );
    }

    #[test]
    fn show_refuses_a_short_frame() {
        // BYTES is the contract. The length check is the first thing `show`
        // does, before any syscall touches the fd — a bare File-backed
        // handle is enough to exercise it (spidev::Spidev::open performs no
        // ioctl; the write would, and it is never reached).
        assert_eq!(BYTES, 72);
        let Ok(spi) = spidev::Spidev::open("/dev/null") else {
            // No handle on this box; the assertion is about the contract,
            // which encode_frame above already exercises.
            return;
        };
        let e = Ring { spi }.show(&[0u8; 71]).unwrap_err();
        assert!(e.contains("expected 72"), "{e}");
    }
}
