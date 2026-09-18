//! baro — the barometric pressure sensor (BMP180-class, XC3702) on I²C 0x77.
//!
//! CARRIED FORWARD from rust/loa-weather/src/baro.rs (7db9055 — "the maths
//! pinned to the Python driver's own output"). BMP180, NOT BMP280: different
//! chip id (0x55), different registers, different compensation. Getting that
//! wrong reads plausible numbers that are simply wrong, which is worse than
//! reading nothing.
//!
//! Raw i²c chardev (`/dev/i2c-N` + the I2C_SLAVE ioctl), the same way the
//! Python driver did it — no smbus, no crate that wants a C library at build
//! time. The only thing here that could hang is the chip, and every path
//! returns an error rather than waiting on it.
//!
//! THE ONE FIX THIS CARRY MAKES: the history's `compensate` used Rust's `/`,
//! which TRUNCATES toward zero, while the Python's `//` floors. The two agree
//! on the body's own calibration at room temperature — the history's pinned
//! vector proves that — but they diverge below freezing, where the datasheet
//! maths runs negative intermediates: a trunc-diving port returns a pressure
//! 2 Pa off at -5 C with no error raised. `fdiv` (div_euclid) matches the
//! Python exactly at every temperature; both vectors below pin the match, and
//! the low-temp vector also pins the divergence the old shape would have had.
use std::fs::{File, OpenOptions};
use std::io::{Read, Write};
use std::os::unix::io::AsRawFd;

pub const DEFAULT_BUS: i32 = 1;
pub const DEFAULT_ADDR: i32 = 0x77; // XC3702 sits at 0x77 (SDO high) — verified 09-12
/// oss=0: 4.5ms per the datasheet; 5ms covers it.
const MEAS_WAIT: std::time::Duration = std::time::Duration::from_millis(5);
const I2C_SLAVE: libc::c_ulong = 0x0703;

pub struct Baro {
    path: String,
    addr: i32,
    cal: Option<[i64; 11]>,
}

impl Baro {
    pub fn open(bus: i32, addr: i32) -> Self {
        Self { path: format!("/dev/i2c-{bus}"), addr, cal: None }
    }

    fn device(&self) -> Result<File, String> {
        let f = OpenOptions::new()
            .read(true)
            .write(true)
            .open(&self.path)
            .map_err(|e| format!("{}: {e}", self.path))?;
        // Which slave this file descriptor talks to. Set once per open, as the
        // Python did: the fd carries the address, not the calls.
        let rc = unsafe { libc::ioctl(f.as_raw_fd(), I2C_SLAVE, self.addr) };
        if rc < 0 {
            return Err(format!("{}: I2C_SLAVE 0x{:02x} refused", self.path, self.addr));
        }
        Ok(f)
    }

    /// 22 bytes of calibration from 0xAA, once. Big-endian signed pairs, with
    /// ac4/ac5/ac6 taken as unsigned — that is what the datasheet means by
    /// `unsigned short`, and taking them signed makes the pressure drift low.
    fn cal(&mut self, f: &mut File) -> Result<[i64; 11], String> {
        if let Some(c) = self.cal {
            return Ok(c);
        }
        let d = read_reg(f, 0xAA, 22)?;
        if d.len() != 22 {
            return Err(format!("calibration short: {} bytes", d.len()));
        }
        let s = |i: usize| -> i64 { i16::from_be_bytes([d[i], d[i + 1]]) as i64 };
        let u = |i: usize| -> i64 { u16::from_be_bytes([d[i], d[i + 1]]) as i64 };
        let cal = [s(0), s(2), s(4), u(6), u(8), u(10),
                   s(12), s(14), s(16), s(18), s(20)];
        self.cal = Some(cal);
        Ok(cal)
    }

    /// One reading: (temp_c, pressure_pa).
    pub fn read(&mut self) -> Result<(f64, f64), String> {
        let mut f = self.device()?;
        let cal = self.cal(&mut f)?;
        write_reg(&mut f, 0xF4, 0x2E)?; // temp, oss=none
        std::thread::sleep(MEAS_WAIT);
        let t = read_reg(&mut f, 0xF6, 2)?;
        let ut = u16::from_be_bytes([t[0], t[1]]);
        write_reg(&mut f, 0xF4, 0x34)?;            // pressure, oss=0
        std::thread::sleep(MEAS_WAIT);
        let raw = read_reg(&mut f, 0xF6, 3)?;
        let up = (((raw[0] as i64) << 16) | ((raw[1] as i64) << 8)
            | raw[2] as i64) >> 8;
        Ok(compensate(&cal, ut as i64, up))
    }
}

fn write_reg(f: &mut File, reg: u8, val: u8) -> Result<(), String> {
    f.write_all(&[reg, val]).map_err(|e| e.to_string())
}

fn read_reg(f: &mut File, reg: u8, n: usize) -> Result<Vec<u8>, String> {
    f.write_all(&[reg]).map_err(|e| e.to_string())?;
    let mut buf = vec![0u8; n];
    f.read_exact(&mut buf).map_err(|e| e.to_string())?;
    Ok(buf)
}

/// Floor division — Python's `//` on integers. Rust's `/` truncates toward
/// zero; on the datasheet maths that is a silent Pa-level error the moment
/// any intermediate goes negative (cold readings, and the x2 term almost
/// always does).
fn fdiv(a: i64, b: i64) -> i64 {
    a.div_euclid(b)
}

/// The BMP180/BMP085 datasheet compensation, oss=0 → (temp_c, pressure_pa).
///
/// i64 THROUGHOUT, and that is not laziness: the datasheet's maths is written
/// for 32-bit ints and overflows them in the b6/b7 terms for perfectly
/// ordinary readings. Python silently promoted to bignum; Rust in release
/// would WRAP and hand back a wrong pressure with no error. Same numbers,
/// same order of operations, floor division, wide enough to mean it.
pub fn compensate(cal: &[i64; 11], ut: i64, up: i64) -> (f64, f64) {
    let (ac1, ac2, ac3, ac4, ac5, ac6, b1, b2, _mb, mc, md) =
        (cal[0], cal[1], cal[2], cal[3], cal[4], cal[5], cal[6], cal[7],
         cal[8], cal[9], cal[10]);
    let x1 = ((ut - ac6) * ac5) >> 15;
    let x2 = fdiv(mc << 11, x1 + md);
    let b5 = x1 + x2;
    let temp_c = ((b5 + 8) >> 4) as f64 / 10.0;
    let b6 = b5 - 4000;
    let x1 = (b2 * ((b6 * b6) >> 12)) >> 11;
    let x2 = (ac2 * b6) >> 11;
    let x3 = x1 + x2;
    let b3 = ((ac1 * 4 + x3) + 2) >> 2;
    let x1 = (ac3 * b6) >> 13;
    let x2 = (b1 * ((b6 * b6) >> 12)) >> 16;
    let x3 = ((x1 + x2) + 2) >> 2;
    let b4 = (ac4 * (x3 + 32768)) >> 15;
    let b7 = (up - b3) * 50000;
    let mut p = if b7 < 0x8000_0000 { fdiv(b7 * 2, b4) } else { fdiv(b7, b4) * 2 };
    let x1 = (p >> 8) * (p >> 8);
    let x1 = (x1 * 3038) >> 16;
    let x2 = (-7357 * p) >> 16;
    p += (x1 + x2 + 3791) >> 4;
    (temp_c, p as f64)
}

#[cfg(test)]
mod tests {
    use super::compensate;

    /// The body's own calibration words, read off the XC3702 on the bench
    /// (2026-09-14) — carried with the history's vector.
    fn body_cal() -> [i64; 11] {
        [8687, -1183, -14304, 33899, 25081, 20813, 6515, 47,
         -32768, -11786, 2771]
    }

    /// Calibration and raw values read off the body's own XC3702 (2026-09-14),
    /// with the value the PYTHON driver produced for the same inputs:
    /// temp_c = 22.9, pa = 102276 (1022.76 hPa).
    ///
    /// Pinned to the Python's OUTPUT rather than to a range, because "roughly
    /// right" is exactly the failure this port must not have: a compensation
    /// wrong by a constant reads as weather.
    #[test]
    fn the_compensation_matches_the_python_driver_at_room_temperature() {
        let (t, p) = compensate(&body_cal(), 29065, 43571);
        assert_eq!(t, 22.9, "temperature does not match the Python driver");
        assert_eq!(p, 102276.0, "pressure does not match the Python driver");
    }

    /// A second vector at the same cal, synthesised with the Python
    /// `_compensate` (the reference's own function) on 2026-09-18. The
    /// history's test carried only the room-temperature case; this one
    /// lands where (mc << 11) // (x1 + md) runs the division NEGATIVE,
    /// the case Rust's truncating `/` gets wrong:
    /// temp_c = -5.2, pa = 73331. A trunc-diving port returns 73333 —
    /// a 2 Pa error with no error raised, i.e. weather invented from maths.
    #[test]
    fn the_compensation_matches_the_python_driver_below_freezing_where_the_division_goes_negative() {
        let (t, p) = compensate(&body_cal(), 25000, 36000);
        assert_eq!(t, -5.2, "floor-shifted temp must still match the Python");
        assert_eq!(p, 73331.0, "trunc vs floor division: the Pa-level lie");
    }

    /// And the history's arithmetic-shape case kept honest: ut=27000/up=40000
    /// through the Python produced 9.5 C / 88233 Pa — positive-path, both
    /// division shapes agree, pinned so a future refactor cannot drift it.
    #[test]
    fn the_compensation_matches_a_warm_synthetic_vector() {
        let (t, p) = compensate(&body_cal(), 27000, 40000);
        assert_eq!((t, p), (9.5, 88233.0));
    }

    #[test]
    fn a_missing_bus_is_an_error_not_a_panic_on_this_x86_box() {
        // No /dev/i2c-* here: read() must return Err naming the path. This is
        // the daemon's hardware-absent path.
        let mut b = super::Baro::open(1, super::DEFAULT_ADDR);
        let e = b.read().expect_err("no i2c bus on x86");
        assert!(e.contains("/dev/i2c-1"), "the error must name the door: {e}");
    }
}
