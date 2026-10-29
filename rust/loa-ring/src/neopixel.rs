//! The WS2812B, over SPI, on the Pi 5.
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

pub const LEDS: usize = 24;
pub const BYTES: usize = LEDS * 3;             // 72: 24 px RGB
const SPI_BYTE_PER_WS_BIT: usize = 1;
const LATCH: usize = 24;                       // 60us low at 3.2MHz

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
/// `tests/wire_format.rs` pins it against the Python encoder's own bytes.
pub fn ws_byte(v: u8, out: &mut Vec<u8>) {
    for bit in (0..8).rev() {
        out.push(if (v >> bit) & 1 == 1 { ONE } else { ZERO });
    }
}

pub struct Ring {
    spi: spidev::Spidev,
}

impl Ring {
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

    /// 72 bytes of RGB (r,g,b per LED) -> the strip. GRB on the wire: the
    /// WS2812B takes green first, which is a property of the part and not
    /// something the renderer has to know about.
    pub fn show(&mut self, rgb: &[u8]) -> Result<(), String> {
        if rgb.len() != BYTES {
            return Err(format!("frame is {} bytes, expected {BYTES}", rgb.len()));
        }
        let mut buf = Vec::with_capacity(BYTES * 8 * SPI_BYTE_PER_WS_BIT + LATCH);
        for px in rgb.chunks_exact(3) {
            ws_byte(px[1], &mut buf);          // G
            ws_byte(px[0], &mut buf);          // R
            ws_byte(px[2], &mut buf);          // B
        }
        buf.extend(std::iter::repeat_n(0u8, LATCH));   // latch: 60us low
        self.spi.write_all(&buf).map_err(|e| e.to_string())
    }

    pub fn off(&mut self) -> Result<(), String> {
        self.show(&[0u8; BYTES])
    }
}
