//! The SH1106, over SPI0, with DC and RES held open.
//!
//! WRITE-ONLY, like the Python driver it replaces: the panel cannot be read
//! back, so whatever it was last told is remembered HERE. Nothing in this file
//! derives a picture or reads a file — the bytes come off the wire and go to
//! the glass.
//!
//! DC and RES are claimed ONCE through liblgpio, loaded at runtime. The Python
//! daemon spawned `pinctrl` for every transfer at first — 16 processes a frame,
//! 1.40ms each, 22.5ms, a 45fps ceiling that had nothing to do with the panel.
//! Holding the lines open measures 371fps against a 472fps wire.

use std::io::Write;

pub const WIDTH: usize = 128;
pub const PAGES: usize = 8;
pub const BYTES: usize = WIDTH * PAGES;
pub const HEIGHT: usize = 64;

const DC: i32 = 25;
const RES: i32 = 24;

/// Panel contrast register values. A DISPLAY characteristic: the cortex says
/// which, the glass applies it.
pub const BRIGHT: i32 = 0xCF;
pub const DIM: i32 = 0x18;

/// The init sequence, as measured on this glass. Orientation is NOT in here —
/// it is a setting, applied through `set_flip`, so one place decides it.
const INIT: [u8; 26] = [
    0xAE, 0xD5, 0x80, 0xA8, 0x3F, 0xD3, 0x00, 0x40, 0x8D, 0x14, 0x20, 0x00, 0xDA,
    0x12, 0x81, 0xCF, 0xD9, 0xF1, 0xDB, 0x40, 0xA4, 0xA6, 0x2E, 0xA0, 0xC0, 0xAF,
];

pub const FLIP_ON: [u8; 2] = [0xA1, 0xC8];
pub const FLIP_OFF: [u8; 2] = [0xA0, 0xC0];

type GpioChipOpen = unsafe extern "C" fn(i32) -> i32;
type GpioClaimOutput = unsafe extern "C" fn(i32, i32, i32, i32) -> i32;
type GpioWrite = unsafe extern "C" fn(i32, i32, i32) -> i32;
type GpioFree = unsafe extern "C" fn(i32, i32) -> i32;
type GpioChipClose = unsafe extern "C" fn(i32) -> i32;

/// liblgpio, by dlopen. The library is on the Pi already (the ring uses it), so
/// the body needs no new package, no headers, and nothing at link time.
struct Lgpio {
    _lib: libloading::Library,
    handle: i32,
    write_fn: GpioWrite,
    free_fn: GpioFree,
    close_fn: GpioChipClose,
    dc: i32,
    res: i32,
}

impl Lgpio {
    fn open(chip: i32) -> Result<Self, String> {
        unsafe {
            let lib = libloading::Library::new("liblgpio.so.1")
                .map_err(|e| format!("liblgpio.so.1: {e}"))?;
            let chip_open: libloading::Symbol<GpioChipOpen> =
                lib.get(b"lgGpiochipOpen\0").map_err(|e| e.to_string())?;
            let claim: libloading::Symbol<GpioClaimOutput> =
                lib.get(b"lgGpioClaimOutput\0").map_err(|e| e.to_string())?;
            let write_fn: GpioWrite =
                *lib.get(b"lgGpioWrite\0").map_err(|e| e.to_string())?;
            let free_fn: GpioFree =
                *lib.get(b"lgGpioFree\0").map_err(|e| e.to_string())?;
            let close_fn: GpioChipClose =
                *lib.get(b"lgGpiochipClose\0").map_err(|e| e.to_string())?;

            let handle = chip_open(chip);
            if handle < 0 {
                return Err(format!("lgGpiochipOpen({chip}) -> {handle}"));
            }
            for pin in [DC, RES] {
                let rc = claim(handle, 0, pin, 1);
                if rc < 0 {
                    return Err(format!("lgGpioClaimOutput({pin}) -> {rc}"));
                }
            }
            Ok(Lgpio {
                _lib: lib,
                handle,
                write_fn,
                free_fn,
                close_fn,
                dc: 1,
                res: 1,
            })
        }
    }

    /// Write a line only when it changes: a syscall per identical level is a
    /// syscall the panel never asked for.
    fn set(&mut self, pin: i32, level: i32) {
        let slot = if pin == DC { &mut self.dc } else { &mut self.res };
        if *slot == level {
            return;
        }
        unsafe { (self.write_fn)(self.handle, pin, level) };
        *slot = level;
    }
}

impl Drop for Lgpio {
    fn drop(&mut self) {
        unsafe {
            (self.free_fn)(self.handle, DC);
            (self.free_fn)(self.handle, RES);
            (self.close_fn)(self.handle);
        }
    }
}

pub struct Panel {
    spi: spidev::Spidev,
    gpio: Lgpio,
    flip: Option<bool>,
    contrast: i32,
}

impl Panel {
    pub fn open(path: &str, speed_hz: u32, chip: i32) -> Result<Self, String> {
        let mut spi = spidev::Spidev::open(path).map_err(|e| format!("{path}: {e}"))?;
        let opts = spidev::SpidevOptions::new()
            .bits_per_word(8)
            .max_speed_hz(speed_hz)
            .mode(spidev::SpiModeFlags::SPI_MODE_0)
            .build();
        spi.configure(&opts).map_err(|e| e.to_string())?;
        let mut gpio = Lgpio::open(chip)?;

        gpio.set(RES, 0);
        std::thread::sleep(std::time::Duration::from_millis(50));
        gpio.set(RES, 1);
        std::thread::sleep(std::time::Duration::from_millis(50));

        let mut p = Panel {
            spi,
            gpio,
            flip: None,
            contrast: BRIGHT,
        };
        for c in INIT {
            p.cmd(&[c])?;
        }
        // Orientation is applied from DEFAULT_FLIP so the glass is right at
        // power-on, not whenever the feed happens to arrive.
        p.set_flip(false)?;
        Ok(p)
    }

    fn cmd(&mut self, bytes: &[u8]) -> Result<(), String> {
        self.gpio.set(DC, 0);
        self.spi.write_all(bytes).map_err(|e| e.to_string())
    }

    fn data(&mut self, bytes: &[u8]) -> Result<(), String> {
        self.gpio.set(DC, 1);
        self.spi.write_all(bytes).map_err(|e| e.to_string())
    }

    /// One frame: 8 pages, each a 3-byte address then 128 bytes of pixels.
    /// SH1106 is page-addressed only (horizontal addressing is the SSD1306), so
    /// the page pointer cannot wrap and the commands per page are not optional.
    pub fn show(&mut self, buf: &[u8]) -> Result<(), String> {
        for page in 0..PAGES {
            let a = 0xB0 | page as u8;
            self.cmd(&[a, 0x02, 0x10])?;
            self.data(&buf[page * WIDTH..(page + 1) * WIDTH])?;
        }
        Ok(())
    }

    pub fn set_flip(&mut self, flipped: bool) -> Result<(), String> {
        if self.flip == Some(flipped) {
            return Ok(());
        }
        let seq = if flipped { FLIP_ON } else { FLIP_OFF };
        for c in seq {
            self.cmd(&[c])?;
        }
        self.flip = Some(flipped);
        Ok(())
    }

    pub fn set_contrast(&mut self, val: i32) -> Result<(), String> {
        let v = val.clamp(0, 255) as u8;
        if self.contrast == v as i32 {
            return Ok(());
        }
        self.cmd(&[0x81, v])?;
        self.contrast = v as i32;
        Ok(())
    }

    pub fn clear(&mut self) -> Result<(), String> {
        self.show(&[0u8; BYTES])
    }
}
