//! HT16K33 8x8 LED matrix — the chip on the back of the keyestudio panel.
//!
//! The part is a keyboard/display controller with 16 bytes of display RAM and
//! its own oscillator. Everything it needs is a handful of one-byte commands;
//! the frame is 16 bytes at address 0x00, auto-incrementing.
//!
//! **Measured, not assumed (2026-09-13, Divv at the bench).** Three things
//! about this board differ from the obvious reading of the datasheet, and
//! each needed its own test to find:
//!
//!   - the address is a COLUMN, not a row — a full byte draws a vertical line
//!   - the column order is mirrored: 0x00 is the right-hand column as viewed
//!   - the bit order is rotated by one: the top row is bit 7, not bit 0
//!
//! The upshot is that a picture drawn with the assumed row/column split comes
//! out rotated 90°, and no amount of screen rotation can undo that, because a
//! rotation cannot fix an axis swap. See `ram()` for the mapping that holds.
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

/// 16 bytes of display RAM. This board uses the eight even ones, one per
/// column; the odd ones are never written — whether anything is wired to them
/// is unverified and it does not matter, because nothing drives them.
const FRAME_LEN: usize = 16;

/// The measured RAM layout for this board.
///
/// Bench-verified 2026-09-13 and it is NOT the datasheet reading. Three
/// separate things were wrong in the obvious mapping, each found by a test
/// that could only have caught that one:
///
///   byte = 2 * (7 - x)   x = 0 is the left-hand column as viewed
///   bit  = (y + 7) % 8   y = 0 is the top row as viewed
///
/// The address is a COLUMN, not a row (the column sweep). The column order is
/// mirrored. And the bit order is ROTATED BY ONE — the last bit drives the
/// top row, not the bottom (the bit sweep). A rotation cannot fix an axis
/// swap and neither can fix the wrap, which is why all three got their own
/// test instead of being reasoned about.
///
/// Odd addresses are never written: on this panel they drive nothing.
fn ram(x: usize, y: usize) -> (usize, u8) {
    (2 * (7 - x), 1u8 << ((y + 7) % 8))
}

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
        let (byte, bit) = ram(x, y);
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

    /// Hold one raw byte, nothing else on the panel.
    pub fn raw(&mut self, idx: usize, value: u8, hold: Duration) -> io::Result<()> {
        println!("raw RAM 0x{idx:02x} = 0x{value:02x}, held");
        sleep(Duration::from_secs(2));
        self.clear();
        self.byte(idx, value);
        self.flush()?;
        sleep(hold);
        self.clear();
        self.flush()
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

    /// Which bit drives which physical row: one horizontal line per bit.
    ///
    /// The address sweep proved the addresses are columns; this proves the
    /// other half — which of the 8 bits inside a byte is which position down
    /// it. A bit set across all eight columns is a horizontal line, which is
    /// a thing a human can name without a coordinate system. Bit n blinks
    /// n+1 times.
    pub fn bit_sweep(&mut self, hold: Duration) -> io::Result<()> {
        println!("bit sweep — one horizontal line per bit, 1..8 blinks");
        sleep(Duration::from_secs(2));
        for bit in 0..8usize {
            let blinks = bit + 1;
            println!("  bit {bit} -> {blinks} blink(s)");
            for _ in 0..blinks {
                self.clear();
                for col in 0..8 {
                    self.byte(col * 2, 1u8 << bit);
                }
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

    /// Walk one line down the panel, then repeat.
    ///
    /// No blinking and no counts: a single line, moving one row at a time from
    /// our row 0 to our row 7, then a pause and again. The only question it
    /// asks is whether the walk starts on the top row of the glass or one
    /// below it — and if it starts one below, the bit order is rotated inside
    /// the module's wiring and the driver has to carry that fact.
    pub fn walk(&mut self, hold: Duration, loops: u32) -> io::Result<()> {
        println!("walk — one line from our row 0 down to row 7, {loops} pass(es)");
        sleep(Duration::from_secs(2));
        for pass in 1..=loops {
            println!("pass {pass}/{loops}");
            for y in 0..8usize {
                self.clear();
                for x in 0..8 {
                    self.set(x, y, true);
                }
                self.flush()?;
                sleep(hold);
            }
            self.clear();
            self.flush()?;
            sleep(Duration::from_secs(3));
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
