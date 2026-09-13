//! loa-gpio — the pins, through liblgpio, without a build-time C dependency.
//!
//! WHY dlopen RATHER THAN A CRATE AGAINST libgpiod. The body already has
//! liblgpio (the ring's own library) and the panel already drives DC and RES
//! through it. Binding at RUNTIME means the daemons build anywhere — dixie, the
//! body, a test container — with no headers, no pkg-config, and no cross
//! toolchain, which is the trap the libzmq work fell into. The cost is that a
//! missing library is a runtime error rather than a build error, so `open`
//! reports exactly what it could not load.
//!
//! ONE IMPLEMENTATION. The panel had its own copy of this and the senses were
//! about to grow a second; two copies of "how to toggle a pin" is the same
//! disease as two copies of a wire format.
use std::ffi::c_char;

type GpiochipOpen = unsafe extern "C" fn(i32) -> i32;
type GpiochipClose = unsafe extern "C" fn(i32) -> i32;
type ClaimInput = unsafe extern "C" fn(i32, i32, i32) -> i32;
type ClaimOutput = unsafe extern "C" fn(i32, i32, i32, i32) -> i32;
type GpioRead = unsafe extern "C" fn(i32, i32) -> i32;
type GpioWrite = unsafe extern "C" fn(i32, i32, i32) -> i32;
type GpioFree = unsafe extern "C" fn(i32, i32) -> i32;

/// The Pi 5's RP1 exposes its GPIO on chip 0 through lgpio. Measured on the
/// body: chip 0 drives GPIO25, which is the panel's DC line, and the pin
/// reflects the level through `pinctrl`.
pub const DEFAULT_CHIP: i32 = 0;

pub struct Chip {
    /// The library must outlive the function pointers taken from it.
    _lib: libloading::Library,
    handle: i32,
    close: GpiochipClose,
    claim_in: ClaimInput,
    claim_out: ClaimOutput,
    read: GpioRead,
    write: GpioWrite,
    free: GpioFree,
}

impl Chip {
    /// Open a gpiochip. `lib` defaults to `liblgpio.so.1`.
    pub fn open(chip: i32) -> Result<Self, String> {
        let path = std::env::var("LOA_LGPIO").unwrap_or_else(|_| "liblgpio.so.1".into());
        Self::open_with(&path, chip)
    }

    pub fn open_with(lib_path: &str, chip: i32) -> Result<Self, String> {
        unsafe {
            let lib = libloading::Library::new(lib_path)
                .map_err(|e| format!("cannot load {lib_path}: {e}"))?;
            macro_rules! sym {
                ($name:literal, $ty:ty) => {
                    *lib.get::<$ty>(concat!($name, "\0").as_bytes())
                        .map_err(|e| format!("{lib_path}: no {}: {e}", $name))?
                };
            }
            let open: GpiochipOpen = sym!("lgGpiochipOpen", GpiochipOpen);
            let close = sym!("lgGpiochipClose", GpiochipClose);
            let claim_in = sym!("lgGpioClaimInput", ClaimInput);
            let claim_out = sym!("lgGpioClaimOutput", ClaimOutput);
            let read = sym!("lgGpioRead", GpioRead);
            let write = sym!("lgGpioWrite", GpioWrite);
            let free = sym!("lgGpioFree", GpioFree);
            let handle = open(chip);
            if handle < 0 {
                return Err(format!("lgGpiochipOpen({chip}) = {handle}"));
            }
            Ok(Self {
                _lib: lib,
                handle,
                close,
                claim_in,
                claim_out,
                read,
                write,
                free,
            })
        }
    }

    /// Claim a pin as an input. Returns the error liblgpio gave, not a guess:
    /// EBUSY here means something else owns the line, which is a real fault on
    /// a bench with jumpers on it.
    pub fn claim_input(&self, gpio: i32) -> Result<(), String> {
        let rc = unsafe { (self.claim_in)(self.handle, 0, gpio) };
        if rc < 0 {
            return Err(format!("claim_input({gpio}) = {rc}"));
        }
        Ok(())
    }

    pub fn claim_output(&self, gpio: i32, level: i32) -> Result<(), String> {
        let rc = unsafe { (self.claim_out)(self.handle, 0, gpio, level) };
        if rc < 0 {
            return Err(format!("claim_output({gpio}) = {rc}"));
        }
        Ok(())
    }

    /// 0 or 1. A NEGATIVE return is an error and is reported as one rather than
    /// read as a level: treating -1 as "high" would invent a motion event.
    pub fn read(&self, gpio: i32) -> Result<i32, String> {
        let v = unsafe { (self.read)(self.handle, gpio) };
        if v < 0 {
            return Err(format!("read({gpio}) = {v}"));
        }
        Ok(v)
    }

    pub fn write(&self, gpio: i32, level: i32) -> Result<(), String> {
        let rc = unsafe { (self.write)(self.handle, gpio, level) };
        if rc < 0 {
            return Err(format!("write({gpio}, {level}) = {rc}"));
        }
        Ok(())
    }

    pub fn free(&self, gpio: i32) -> Result<(), String> {
        let rc = unsafe { (self.free)(self.handle, gpio) };
        if rc < 0 {
            return Err(format!("free({gpio}) = {rc}"));
        }
        Ok(())
    }
}

impl Drop for Chip {
    fn drop(&mut self) {
        unsafe { (self.close)(self.handle) };
    }
}

/// Kept so the unused-import lint does not fire on a type used only in the
/// signature macro above; c_char is part of the ABI story if a caller needs to
/// hand liblgpio a path.
#[allow(dead_code)]
fn _c_char_in_abi(_: *const c_char) {}
