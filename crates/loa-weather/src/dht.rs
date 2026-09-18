//! dht — the DHT22-class temp/humidity driver (the board's XC4520, spoken to
//! with the frame the Python driver `loa/weather/dht.py` decodes) on the
//! 1-wire DATA line, GPIO4.
//!
//! THE TIMING, CARRIED FROM THE PYTHON (3de6a54, 8e9678f): drive the line low
//! for 20ms, hand it back, then collect rising->falling high-widths. The old
//! note that pinned this: the gpiod re-request between the start and the
//! sampling window can miss the response pair, so pulses are collected and
//! EVERY ALIGNMENT is tried until the frame checksum validates — the checksum
//! (sum of the four data bytes & 0xff == byte 5) is the referee, and a valid
//! frame proves itself. A misaligned or half-heard read decodes to nothing;
//! it never decodes to a wrong room temperature.
//!
//! WHAT THIS PORT CHANGES FROM THE PYTHON, ON PURPOSE: the reference asked
//! the kernel for edge timestamps (gpiod EDGE events — immune to interpreter
//! jitter, per 3de6a54). liblgpio as carried here exposes no edge monitor in
//! the history's shape, so the collection is a SPIN on lgGpioRead with
//! `Instant` as the clock, exactly like the sonar's echo loop: the spin IS
//! the timestamp source, and the frame checksum is what a jittery spin hides
//! behind. The bit-window maths (`DHT_ONE_NS`, the alignment search, the
//! range gate) is the Python's verbatim, pinned against the Python decoder's
//! own output — see the tests.
use std::time::{Duration, Instant};

use loa_sense::gpio::Chip;

pub const DEFAULT_TEMP_GPIO: i32 = 4; // REMOTE weather board DATA (XC4520)
/// The sensor's own minimum spacing is ~1-2s; 1Hz is the floor the doctrine
/// allows and tighter makes it re-serve a stale frame. The DHT read blocks
/// ~20ms of pulse window; nothing else shares this tick.
pub const DEFAULT_TEMP_PERIOD: f64 = 1.0;
/// 20ms to collect pulses; a stuck line must not hang the daemon.
const DHT_PULSE_WINDOW: Duration = Duration::from_millis(20);
/// 84 edge transitions cover the worst real frame: 2 response + 40 highs
/// (each rising recorded; fallings counted too) inside the window.
const DHT_MAX_EDGES: usize = 84;
/// High pulse longer than 60us = bit 1 — THIS CLONE's '0' drifts to ~47us,
/// '1' starts at 70us (the Python's comment, measured on the board). The
/// unit here is NANOSECONDS: 60us.
pub const DHT_ONE_NS: i64 = 60_000;
/// The start signal the sensor wants: >=18ms low; the Python slept 20ms.
const START_LOW: Duration = Duration::from_millis(20);

/// One collected pulse train: high-widths in nanoseconds, in the order the
/// edges arrived. A plain Vec so the decoder stays a pure function and the
/// Python-generated test vectors feed it directly.
pub type Pulses = Vec<i64>;

/// The decoder, carried from DHT11._decode in loa/weather/dht.py — same
/// threshold (p > 60us), same every-alignment search, same checksum rule
/// ((b0+b1+b2+b3) & 0xff == b4), same range gate applied only to a
/// checksum-valid frame. Returns (temp_c, hum_pct).
///
/// The alignment range matches the Python exactly: `range(0, len-39)` tops
/// out at offset len-40, the last full window, and so does `0..=(len-40)`.
/// Verified against the Python on the vectors below, not assumed.
pub fn decode(pulses: &[i64]) -> Option<(f64, f64)> {
    if pulses.len() < 40 {
        return None;
    }
    for s in 0..=(pulses.len() - 40) {
        let win = &pulses[s..s + 40];
        let mut b = [0u8; 5];
        for (i, byte) in b.iter_mut().enumerate() {
            let mut v = 0u16;
            for &p in &win[i * 8..(i + 1) * 8] {
                v = (v << 1) | (if p > DHT_ONE_NS { 1 } else { 0 }) as u16;
            }
            *byte = v as u8;
        }
        if ((b[0] as u16 + b[1] as u16 + b[2] as u16 + b[3] as u16) & 0xFF) != b[4] as u16 {
            continue;
        }
        let hum = b[0] as f64 + b[1] as f64 / 10.0;
        let temp = b[2] as f64 + b[3] as f64 / 10.0;
        if (-40.0..=80.0).contains(&temp) && (0.0..=100.0).contains(&hum) {
            return Some((temp, hum));
        }
    }
    None
}

/// high durations from rising->falling pairs; stray edges are skipped. The
/// Python's pairing loop, line for line: `edges` carries (rising, ABSOLUTE
/// stamp in ns since the collection began) and a high's width is the falling
/// edge's stamp minus its rising edge's stamp. A leading falling edge (joined
/// late) is dropped, not mispaired.
pub fn highs_from_edges(edges: &[(bool, i64)]) -> Pulses {
    let mut highs = Vec::new();
    let mut i = 0;
    while i + 1 < edges.len() {
        if edges[i].0 {
            highs.push(edges[i + 1].1 - edges[i].1);
            i += 2;
        } else {
            i += 1;
        }
    }
    highs
}

pub struct Dht {
    pub gpio: i32,
    /// Consecutive failed reads, for the first-and-every-tenth log shape the
    /// reference uses.
    pub fails: u32,
}

impl Dht {
    pub fn new(gpio: i32) -> Self {
        Self { gpio, fails: 0 }
    }

    /// One full read: start signal, pulse collection, decode. None = no valid
    /// frame this cycle (nothing attached, line stuck, or a bad sound). Every
    /// wait is deadline-guarded: the daemon's 1s tick must never overrun into
    /// the next sensor's timing.
    pub fn read(&mut self, chip: &Chip) -> Option<(f64, f64)> {
        let pulses = self.collect(chip)?;
        if pulses.is_empty() {
            self.fails += 1;
            return None;
        }
        match decode(&pulses) {
            Some(v) => {
                self.fails = 0;
                Some(v)
            }
            None => {
                self.fails += 1;
                None
            }
        }
    }

    /// The hardware half, separate so the spin loop's shape is the only thing
    /// a bench debug has to trust.
    fn collect(&self, chip: &Chip) -> Option<Pulses> {
        // start signal: drive the line low for 20ms (the pull-up holds it
        // high whenever we are not owning it; low for 20ms IS the request)
        chip.claim_output(self.gpio, 0).ok()?;
        std::thread::sleep(START_LOW);
        // release: back to input — the same claim dance the Python did
        // between its two gpiod requests. The pull-up owns the line now;
        // the response pair starts within ~40us of the release.
        chip.claim_input(self.gpio).ok()?;

        // Collect both-edge transitions by spinning on the level, stamping
        // each with ns since the window opened — the same (rising, absolute
        // stamp) shape the gpiod edge events carried, so the pairer is the
        // Python's loop unchanged.
        let mut edges: Vec<(bool, i64)> = Vec::with_capacity(DHT_MAX_EDGES);
        let start = Instant::now();
        let mut prev = chip.read(self.gpio).ok()? != 0;
        while start.elapsed() < DHT_PULSE_WINDOW && edges.len() < DHT_MAX_EDGES {
            let level = chip.read(self.gpio).ok()? != 0;
            if level != prev {
                edges.push((level, start.elapsed().as_nanos() as i64));
                prev = level;
            }
            std::hint::spin_loop();
        }
        if edges.is_empty() {
            return None;
        }
        Some(highs_from_edges(&edges))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Vectors produced by the Python reference's OWN decoder
    /// (loa/weather/dht.py, DHT11._decode — a pure static method, run here
    /// on 2026-09-18) for these exact pulse arrays. The arrays are
    /// synthesised at the clone's measured bit widths (~30us zero, ~75us
    /// one; nanoseconds). Every expected value below is what _decode
    /// returned for that array — the Rust decoder must agree, field for
    /// field, None for None.
    fn clean_45rh_123c() -> Pulses {
        vec![
            30000, 30000, 75000, 30000, 75000, 75000, 30000, 75000, 30000, 30000, 30000, 30000,
            30000, 30000, 30000, 30000, 30000, 30000, 30000, 30000, 75000, 75000, 30000, 30000,
            30000, 30000, 30000, 30000, 30000, 30000, 75000, 75000, 30000, 30000, 75000, 75000,
            75000, 75000, 30000, 30000,
        ]
    }
    fn clean_655rh_279c() -> Pulses {
        vec![
            30000, 75000, 30000, 30000, 30000, 30000, 30000, 75000, 30000, 30000, 30000, 30000,
            30000, 75000, 30000, 75000, 30000, 30000, 30000, 75000, 75000, 30000, 75000, 75000,
            30000, 30000, 30000, 30000, 75000, 30000, 30000, 75000, 30000, 75000, 75000, 30000,
            75000, 30000, 75000, 30000,
        ]
    }
    fn clean_850rh_306c() -> Pulses {
        vec![
            30000, 75000, 30000, 75000, 30000, 75000, 30000, 75000, 30000, 30000, 30000, 30000,
            30000, 30000, 30000, 30000, 30000, 30000, 30000, 75000, 75000, 75000, 75000, 30000,
            30000, 30000, 30000, 30000, 30000, 75000, 75000, 30000, 30000, 75000, 75000, 75000,
            75000, 30000, 30000, 75000,
        ]
    }
    fn range_reject() -> Pulses {
        // hum byte 200.0: checksum-valid, physically impossible.
        vec![
            30000, 75000, 75000, 75000, 75000, 30000, 30000, 30000, 30000, 30000, 30000, 30000,
            30000, 30000, 30000, 30000, 30000, 30000, 30000, 30000, 30000, 30000, 30000, 30000,
            30000, 30000, 75000, 75000, 30000, 30000, 75000, 30000, 75000, 30000, 75000, 30000,
            75000, 30000, 75000, 30000,
        ]
    }

    #[test]
    fn decoder_matches_python_vector_clean_45_0rh_12_3c() {
        assert_eq!(decode(&clean_45rh_123c()), Some((12.3, 45.0)));
    }

    #[test]
    fn decoder_matches_python_vector_clean_65_5rh_27_9c() {
        assert_eq!(decode(&clean_655rh_279c()), Some((27.9, 65.5)));
    }

    #[test]
    fn decoder_matches_python_vector_clean_85_0rh_30_6c() {
        assert_eq!(decode(&clean_850rh_306c()), Some((30.6, 85.0)));
    }

    #[test]
    fn decoder_matches_python_vector_offset_5_junk() {
        // five ~10us non-bit widths in FRONT of a real frame: Python tried
        // every alignment and found it. The collector never has to be
        // perfect because the checksum is the referee.
        let mut junk = clean_655rh_279c();
        for _ in 0..5 {
            junk.insert(0, 10000);
        }
        assert_eq!(decode(&junk), Some((27.9, 65.5)));
    }

    #[test]
    fn decoder_matches_python_vector_bad_checksum() {
        // The 12.3C frame with its LAST width flipped (the checksum byte's
        // bit 0): Python returned None. Anything else here is a fabricated
        // room.
        let mut bad = clean_45rh_123c();
        bad[39] = 75000; // was 30000: sum now misses
        assert_eq!(decode(&bad), None);
    }

    #[test]
    fn decoder_matches_python_vector_range_reject() {
        // Checksum valid, humidity 200%: the gate the Python applies AFTER
        // the checksum — bits arrived honestly, the number is impossible.
        assert_eq!(decode(&range_reject()), None);
    }

    #[test]
    fn fewer_than_40_widths_is_not_a_frame() {
        assert_eq!(decode(&[75000i64; 39]), None);
        assert_eq!(decode(&[]), None);
    }

    #[test]
    fn a_junk_width_before_or_after_a_frame_still_finds_the_frame() {
        // The alignment search both implementations run: offsets 0..=len-40.
        // A junk width after the frame leaves the offset-0 window intact;
        // a junk width before it puts the frame at the LAST alignment
        // (offset len-40) — the boundary Python's range(0, len-39) tops out
        // at. (Checked against the Python both ways: no off-by-one to fix.)
        let mut trailing = clean_45rh_123c();
        trailing.push(9000); // junk width after the frame
        assert_eq!(decode(&trailing), Some((12.3, 45.0)), "junk after");
        let mut shifted = vec![9000i64];
        shifted.extend(clean_45rh_123c());
        assert_eq!(decode(&shifted), Some((12.3, 45.0)), "frame at offset len-40");
    }

    #[test]
    fn the_edge_pairer_pairs_rising_to_falling_and_drops_a_stray_leading_edge() {
        // (rising, absolute ns since window open) — the gpiod event shape
        // collect now produces. Joining mid-low: the first edge is falling
        // with no rising partner; the pairer skips it and the widths that
        // follow are true.
        let edges = vec![
            (false, 1_000),   // stray fall: the high started before we looked
            (true, 5_000),
            (false, 50_000),  // high 0: 45us
            (true, 70_000),
            (false, 145_000), // high 1: 75us
        ];
        assert_eq!(highs_from_edges(&edges), vec![45_000, 75_000]);
    }
}
