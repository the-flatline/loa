//! HT16K33 8x8 LED matrix — the chip on the back of the keyestudio panel.
//!
//! The part is a keyboard/display controller with 16 bytes of display RAM and
//! its own oscillator. Everything it needs is a handful of one-byte commands;
//! the frame is 16 bytes at address 0x00, auto-incrementing, one word per row.
//!
//! Row layout as the chip expects it: address 0x00 = row 0 low byte, 0x01 =
//! row 0 high byte, 0x02 = row 1 low byte, and so on. An 8x8 panel uses the
//! low byte only, bit 0 = leftmost column. If the corner test says otherwise,
//! the mapping is a constant away — not a rewrite.
//!
//! Nothing here reads back. The chip has no readback for the display; the
//! frame we send is the frame we believe in, which is why a `flush` that
//! errors must be treated as the panel being wrong, not merely slow.

use crate::i2c::I2c;
use std::io;
use std::thread::sleep;
use std::time::Duration;

pub const DEFAULT_ADDR: u8 = 0x70;
pub const DEFAULT_BUS: u32 = 1;

/// System oscillator on. The chip is dead without it — display RAM means
/// nothing while the internal clock is stopped.
const CMD_OSC_ON: u8 = 0x21;
/// Display on, blinking off (bits: 0 = on/off, 1 = blink, 2-3 = blink rate).
const CMD_DISPLAY_ON: u8 = 0x81;
const CMD_DISPLAY_OFF: u8 = 0x80;
/// Blink off, explicitly — a previous owner of the chip may have left it on.
const CMD_BLINK_OFF: u8 = 0xA0;
/// Dimming: 0xE0 | level, level 0-15. Global to the panel, not per-pixel.
const CMD_DIM: u8 = 0xE0;

/// Row addresses double as the chip numbers them: 16 bytes, 8 rows.
const FRAME_LEN: usize = 16;

pub struct Matrix {
    i2c: I2c,
    frame: [u8; FRAME_LEN],
    /// Clockwise rotation in degrees applied to every (x, y) before it lands
    /// in RAM: 0, 90, 180 or 270. The panel is square, and which way is "up"
    /// is the enclosure's business, not the driver's — so the driver takes an
    /// angle instead of baking one in. Raw byte writes (`byte`) ignore it;
    /// only the coordinate API rotates.
    pub rotation: u16,
}

impl Matrix {
    /// Open the panel, wake the oscillator, clear the RAM and set brightness.
    pub fn open(bus: u32, addr: u8, brightness: u8) -> io::Result<Self> {
        let mut m = Self {
            i2c: I2c::open(bus, addr)?,
            frame: [0u8; FRAME_LEN],
            rotation: 0,
        };
        m.cmd(CMD_OSC_ON)?;
        m.cmd(CMD_BLINK_OFF)?;
        m.brightness(brightness)?;
        m.flush()?;
        m.cmd(CMD_DISPLAY_ON)?;
        Ok(m)
    }

    fn cmd(&mut self, byte: u8) -> io::Result<()> {
        self.i2c.write(&[byte])
    }

    /// 0 = dimmest, 15 = full. Full brightness on 64 LEDs is a lot of current
    /// for a 3V3 rail and a lot of glare for a bench — 2 is plenty to start.
    pub fn brightness(&mut self, level: u8) -> io::Result<()> {
        self.cmd(CMD_DIM | (level & 0x0F))
    }

    pub fn display(&mut self, on: bool) -> io::Result<()> {
        self.cmd(if on { CMD_DISPLAY_ON } else { CMD_DISPLAY_OFF })
    }

    pub fn clear(&mut self) {
        self.frame = [0u8; FRAME_LEN];
    }

    pub fn set(&mut self, x: usize, y: usize, on: bool) {
        if x > 7 || y > 7 {
            return;
        }
        let (x, y) = self.rotate(x, y);
        let byte = y * 2; // low byte of each row: 0x00, 0x02, ...
        let bit = 1u8 << x; // bit 0 is the leftmost column
        if on {
            self.frame[byte] |= bit;
        } else {
            self.frame[byte] &= !bit;
        }
    }

    /// Screen coordinates to RAM coordinates, clockwise.
    fn rotate(&self, x: usize, y: usize) -> (usize, usize) {
        match self.rotation % 360 {
            90 => (7 - y, x),
            180 => (7 - x, 7 - y),
            270 => (y, 7 - x),
            _ => (x, y),
        }
    }

    /// Write one chip address directly, bypassing rotation and coordinates.
    /// This is the diagnostic door: it is how the frame layout gets proven.
    pub fn byte(&mut self, idx: usize, value: u8) {
        if idx < FRAME_LEN {
            self.frame[idx] = value;
        }
    }

    pub fn set_all(&mut self, on: bool) {
        for y in 0..8 {
            self.frame[y * 2] = if on { 0xFF } else { 0x00 };
        }
    }

    /// Push the frame. One transaction: address 0x00 then all 16 bytes.
    pub fn flush(&mut self) -> io::Result<()> {
        let mut buf = [0u8; FRAME_LEN + 1];
        buf[0] = 0x00;
        buf[1..].copy_from_slice(&self.frame);
        self.i2c.write(&buf)
    }

    /// Which chip address drives which physical row.
    ///
    /// The frame layout is the one thing about this part that boards disagree
    /// on, and it is invisible from the software side — the chip has no
    /// readback. So it gets measured instead of assumed: light one whole chip
    /// address at a time, full width, and count blinks with your eyes.
    ///
    /// Group 1 = the even addresses 0x00..0x0e, group 2 = the odd ones.
    /// Address n blinks n/2+1 times within its group.
    pub fn row_sweep(&mut self, hold: Duration, group: u8) -> io::Result<()> {
        let base = if group == 2 { 1 } else { 0 };
        println!("row sweep, group {group} — full-width row per chip address");
        sleep(Duration::from_secs(2));
        for i in 0..8usize {
            let addr = base + i * 2;
            let blinks = i + 1;
            println!("  0x{addr:02x} -> {blinks} blink(s)",);
            for _ in 0..blinks {
                self.clear();
                self.byte(addr, 0xFF);
                self.flush()?;
                sleep(hold);
                self.clear();
                self.flush()?;
                sleep(hold / 2);
            }
            sleep(hold);
        }
        Ok(())
    }

    /// The corner identification run, for a panel on fly leads whose
    /// orientation nobody has written down yet.
    ///
    /// Each corner gets a blink count instead of a colour — the panel has one
    /// colour and dims globally, so a count is the only cue that survives
    /// being looked at from the wrong end of the bench.
    ///
    /// `loops` repeats the whole sequence with a pause between passes: the
    /// instrument is a human at a bench who may be looking somewhere else
    /// when pass one starts.
    pub fn corner_test(&mut self, hold: Duration, loops: u32) -> io::Result<()> {
        let corners = [(0usize, 0usize), (0, 7), (7, 0), (7, 7)];
        println!("corner test — {loops} pass(es), starting in 2s");
        sleep(Duration::from_secs(2));
        for pass in 1..=loops {
            println!("pass {pass}/{loops}");
            for (i, (x, y)) in corners.iter().enumerate() {
                let blinks = i + 1;
                println!(
                    "  ({x},{y})  ->  {blinks} blink{}",
                    if blinks == 1 { "" } else { "s" }
                );
                for _ in 0..blinks {
                    self.clear();
                    self.set(*x, *y, true);
                    self.flush()?;
                    sleep(hold);
                    self.clear();
                    self.flush()?;
                    sleep(hold / 3);
                }
                sleep(hold / 2);
            }
            println!("  all four, together");
            self.clear();
            for (x, y) in corners {
                self.set(x, y, true);
            }
            self.flush()?;
            sleep(hold * 2);
            self.clear();
            self.flush()?;
            sleep(hold * 4);
        }
        Ok(())
    }
}
