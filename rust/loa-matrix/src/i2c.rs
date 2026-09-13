//! Raw I2C over the character device — the same door the Python drivers use.
//!
//! `/dev/i2c-N`, then `I2C_SLAVE` to bind an address, then plain read/write.
//! No smbus, no libi2c, no C library to cross-compile: one ioctl through libc
//! is the entire foreign interface.
//!
//! Opening the bus and probing an address are separate things. A bus that
//! opens is not a chip that answers — the kernel only finds out when a
//! transfer fails with EREMOTEIO/EIO, which is why `probe` exists.

use std::fs::{File, OpenOptions};
use std::io;
use std::os::unix::io::AsRawFd;

/// ioctl number to bind the file to one slave address (from linux/i2c-dev.h).
const I2C_SLAVE: libc::c_ulong = 0x0703;

pub struct I2c {
    file: File,
    pub addr: u8,
    pub bus: u32,
}

impl I2c {
    /// Open the bus and claim one address on it.
    pub fn open(bus: u32, addr: u8) -> io::Result<Self> {
        let path = format!("/dev/i2c-{bus}");
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .open(&path)
            .map_err(|e| io::Error::new(e.kind(), format!("{path}: {e}")))?;
        let rc = unsafe { libc::ioctl(file.as_raw_fd(), I2C_SLAVE, addr as libc::c_ulong) };
        if rc < 0 {
            let e = io::Error::last_os_error();
            return Err(io::Error::new(
                e.kind(),
                format!("bus {bus} addr 0x{addr:02x}: {e}"),
            ));
        }
        Ok(Self { file, addr, bus })
    }

    pub fn write(&mut self, bytes: &[u8]) -> io::Result<()> {
        let n = unsafe {
            libc::write(
                self.file.as_raw_fd(),
                bytes.as_ptr() as *const libc::c_void,
                bytes.len(),
            )
        };
        if n < 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(())
    }

    pub fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        let n = unsafe {
            libc::read(
                self.file.as_raw_fd(),
                buf.as_mut_ptr() as *mut libc::c_void,
                buf.len(),
            )
        };
        if n < 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(n as usize)
    }
}

/// Does anything answer at this address on this bus?
///
/// Opening the bus always succeeds; the address is only tested on a transfer.
/// A NAK comes back as EIO (5) or EREMOTEIO (121) — that is "nobody home",
/// not "the bus is broken". Anything else is a real error and gets surfaced.
pub fn probe(bus: u32, addr: u8) -> Result<bool, io::Error> {
    let mut dev = I2c::open(bus, addr)?;
    let mut byte = [0u8; 1];
    match dev.read(&mut byte) {
        Ok(_) => Ok(true),
        Err(e) => match e.raw_os_error() {
            Some(libc::EIO) | Some(121) => Ok(false),
            _ => Err(io::Error::new(
                e.kind(),
                format!("bus {} addr 0x{:02x}: {e}", dev.bus, dev.addr),
            )),
        },
    }
}

/// Every address that answers between 0x03 and 0x77 — the usual scan range.
pub fn scan(bus: u32) -> Result<Vec<u8>, io::Error> {
    let mut found = Vec::new();
    for addr in 0x03u8..=0x77 {
        match probe(bus, addr) {
            Ok(true) => found.push(addr),
            Ok(false) => {}
            // One bad address must not kill the scan — a wedged part on a
            // shared bus shows up as a real error on its own address only.
            Err(e) => eprintln!("  probe 0x{addr:02x}: {e}"),
        }
    }
    Ok(found)
}
