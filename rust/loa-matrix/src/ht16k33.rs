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

use crate::glyphs::{self, Glyph};
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

    /// Draw one glyph from the vocabulary.
    ///
    /// The tables are read the way they're written: row 0 at the top, high bit
    /// leftmost. Everything goes through `set`, so the glyphs inherit the
    /// measured RAM layout and the rotation flag rather than re-deriving the
    /// mapping — one place knows how this panel is wired.
    pub fn glyph(&mut self, g: &Glyph) {
        self.clear();
        for (y, row) in g.rows.iter().enumerate() {
            for x in 0..8 {
                if row & (0x80 >> x) != 0 {
                    self.set(x, y, true);
                }
            }
        }
    }

    /// Show every shape in turn so a human can react to them, then show the
    /// one pairing that can't be drawn — shape plus motion.
    pub fn glyph_demo(&mut self, hold: Duration, loops: u32) -> io::Result<()> {
        println!(
            "vocabulary — {} states, {loops} pass(es)",
            glyphs::ALL.len()
        );
        sleep(Duration::from_secs(2));
        for pass in 1..=loops {
            println!("pass {pass}/{loops}");
            for g in glyphs::ALL {
                println!("  {}", g.name);
                self.glyph(g);
                self.flush()?;
                sleep(hold);
                self.clear();
                self.flush()?;
                sleep(hold / 3);
            }
        }
        println!("  breach, fast flash — for now");
        self.flash(&glyphs::BREACH, Duration::from_millis(120), 8)?;
        println!("  attention, slow flash — advisory");
        self.flash(&glyphs::ATTENTION, Duration::from_millis(700), 3)?;
        println!("  invader, once, then gone");
        self.glyph(&glyphs::INVADER);
        self.flush()?;
        sleep(Duration::from_millis(1500));
        self.clear();
        self.flush()
    }

    /// The same shape, moving: urgency without a second vocabulary.
    fn flash(&mut self, g: &Glyph, on: Duration, times: u32) -> io::Result<()> {
        for _ in 0..times {
            self.glyph(g);
            self.flush()?;
            sleep(on);
            self.clear();
            self.flush()?;
            sleep(on);
        }
        Ok(())
    }

    /// The vignette: brightness expressed as density, in space rather than in
    /// time.
    ///
    /// The chip cannot hold two brightnesses at once, so a gradient has to be
    /// a *pattern*: solid where it should read bright, a checkerboard where it
    /// should read half, a sparse scatter at the edge. It is static — the chip
    /// holds it with no bus traffic and no CPU — and it survives a hard-edged
    /// panel, which a temporal dither does not. Then the same pattern plays at
    /// four global brightness levels, because that channel is free and it is
    /// the one that moves.
    pub fn vignette(&mut self) -> io::Result<()> {
        println!("vignette — density holds the gradient, brightness moves under it");
        sleep(Duration::from_secs(2));
        for level in [2u8, 6, 10, 15] {
            println!("  bright {level}");
            self.brightness(level)?;
            self.clear();
            for y in 0..8usize {
                for x in 0..8usize {
                    // Symmetric distance from the middle, in half-steps: 1, 3,
                    // 5, 7 are the four rings a 8x8 can hold.
                    let k = ((2 * x as i32 - 7).abs()).max((2 * y as i32 - 7).abs());
                    let on = match k {
                        0..=3 => true,         // centre: solid, reads bright
                        5 => (x + y) % 2 == 0, // checkerboard: reads half
                        _ => (x + y) % 4 == 0, // sparse scatter: reads dim
                    };
                    if on {
                        self.set(x, y, true);
                    }
                }
            }
            self.flush()?;
            sleep(Duration::from_millis(2500));
        }
        self.brightness(2)?;
        self.clear();
        self.flush()
    }

    /// How fast can frames actually be pushed at this panel?
    ///
    /// The answer decides whether per-pixel brightness is available in
    /// software: temporal dithering needs several sub-frames per perceived
    /// frame, so the ceiling on greys is this rate divided by the number of
    /// levels. Measure, don't estimate — the bus rate and the ioctl overhead
    /// are both guesses until they're timed.
    pub fn bench(&mut self, frames: u32) -> io::Result<()> {
        // A pattern that changes every frame, so nothing is optimised away and
        // every byte actually moves.
        for y in 0..8 {
            self.set(y, y, true);
        }
        let t0 = std::time::Instant::now();
        for i in 0..frames {
            self.frame[0] = (i & 0xFF) as u8;
            self.flush()?;
        }
        let dt = t0.elapsed();
        let per = dt.as_secs_f64() / frames as f64;
        println!("{frames} frames in {:.3}s", dt.as_secs_f64());
        println!("{:.3} ms/frame  —  {:.0} fps", per * 1000.0, 1.0 / per);
        Ok(())
    }

    /// A controlled load step: every LED on, held, then dark, repeated.
    ///
    /// This is an instrument, not a display. A rail's behaviour under a known,
    /// slow current step is measurable by the PMIC's own slow ADC; a fast
    /// flicker is not. Timestamps go to stdout so a sampler on the other
    /// machine can line its readings up with the phases.
    pub fn step(&mut self, on: Duration, off: Duration, cycles: u32, bright: u8) -> io::Result<()> {
        self.brightness(bright)?;
        println!("step — {cycles} cycles, {on:?} loaded, {off:?} idle, bright {bright}");
        sleep(Duration::from_secs(2));
        for c in 1..=cycles {
            let t = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_secs_f64())
                .unwrap_or(0.0);
            println!("LOAD   {t:.3}");
            self.clear();
            self.set_all(true);
            self.flush()?;
            sleep(on);

            let t = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_secs_f64())
                .unwrap_or(0.0);
            println!("IDLE   {t:.3}");
            self.clear();
            self.flush()?;
            sleep(off);
            println!("cycle {c}/{cycles} done");
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
