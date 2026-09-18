//! The PIR's state machine, with the pin taken out of it.
//!
//! CARRIED from the bench-proven rust/loa-motion (28a7ddb) line for line in
//! behaviour: rising edge publishes high+on_ts, falling edge publishes
//! high=false + last_hold, a rising edge HELD for two consecutive polls fires
//! (count, last_ts, the sense event, and the ring scan record) once per
//! cooldown. The debounce is the pending>=2 rule, not a timer: the PIR line
//! chatters, the poll period smooths it, and a scan must not machine-gun.
//!
//! What a reading is: PARTIAL messages, the fields a transition touched —
//! the cortex merges by PRESENCE, so a field left out keeps its old value.
//! The accumulated union is what `full()` builds for an init answer.
use loa_wire::pb;

/// What this daemon has published so far. It is the daemon's OWN memory: the
/// Python version used to ask the cortex for the count it had itself published.
#[derive(Default, Clone)]
pub struct Remembered {
    pub high: bool,
    pub count: u32,
    pub last_ts: Option<f64>,
    pub last_hold: Option<f64>,
    pub on_ts: Option<f64>,
}

impl Remembered {
    /// The full payload: what an init answer carries.
    pub fn full(&self) -> pb::Pir {
        pb::Pir {
            high: Some(self.high),
            count: Some(self.count),
            last_ts: self.last_ts,
            last_hold: self.last_hold,
            // Only ever set by a rise, exactly as in Python: the falling edge
            // published `on_ts: None`, which `partial` drops rather than sends.
            on_ts: self.on_ts,
            ts: None,
        }
    }
}

/// One kind of news this loop can have: a transition (state) or a confirmed
/// scan (a record — the loop also owes two events on the wire for it).
#[derive(Debug, PartialEq)]
pub enum News {
    /// Publish this partial payload.
    State(pb::Pir),
    /// Publish this count payload, then the (sense, ring) events.
    Fire {
        msg: pb::Pir,
        count: u32,
    },
}

pub struct Pir {
    pub cooldown: f64,
    remembered: Remembered,
    last: bool,
    pending: u32,
    last_fire: f64,
    /// The cursor only the poll loop touches — NOT part of the init answer.
    on_ts: Option<f64>,
}

impl Default for Pir {
    fn default() -> Self {
        Self::new(5.0)
    }
}

impl Pir {
    pub fn new(cooldown: f64) -> Self {
        Self {
            cooldown,
            remembered: Remembered::default(),
            last: false,
            pending: 0,
            last_fire: 0.0,
            on_ts: None,
        }
    }

    pub fn full(&self) -> pb::Pir {
        self.remembered.full()
    }

    /// The pin stopped reporting: the pending streak dies with the level the
    /// poller did not actually observe. (The history reset its cursor here
    /// too, and re-asserted the claim.)
    pub fn reset_pending(&mut self) {
        self.pending = 0;
    }

    /// The level the daemon sees at boot, before any edge: the full pin state
    /// on our own start, so a cortex already up hears the truth without
    /// waiting for something to move. Returns the payload to publish. The
    /// poll cursor is NOT seeded (as in the history): a boot into a held-high
    /// line still needs two consecutive polls to confirm a scan, and the
    /// first of them re-states high with a fresh on_ts — presence merge
    /// makes that idempotent.
    pub fn seed(&mut self, level: bool, now: f64) -> pb::Pir {
        self.remembered.high = level;
        if level {
            self.on_ts = Some(now);
            self.remembered.on_ts = Some(now);
        }
        pb::Pir {
            high: Some(level),
            on_ts: if level { Some(now) } else { None },
            ..Default::default()
        }
    }

    /// One poll, with the level that came off the pin. The None case (the pin
    /// would not read) is the CALLER's branch — a read error also re-asserts
    /// the claim, and that is the hardware's business, not this machine's.
    pub fn tick(&mut self, level: bool, now: f64) -> Vec<News> {
        let mut out = Vec::new();
        if level != self.last {
            if level {
                self.on_ts = Some(now);
                self.remembered.high = true;
                self.remembered.on_ts = Some(now);
                out.push(News::State(pb::Pir {
                    high: Some(true),
                    on_ts: Some(now),
                    ..Default::default()
                }));
            } else {
                let hold = self.on_ts.map(|t| now - t).unwrap_or(0.0);
                self.on_ts = None;
                self.remembered.high = false;
                self.remembered.last_hold = Some(hold);
                out.push(News::State(pb::Pir {
                    high: Some(false),
                    last_hold: Some(hold),
                    ..Default::default()
                }));
            }
        }
        if level {
            self.pending = if self.last { self.pending + 1 } else { 1 };
            if self.pending >= 2 && now - self.last_fire >= self.cooldown {
                self.last_fire = now;
                self.remembered.count += 1;
                self.remembered.last_ts = Some(now);
                out.push(News::Fire {
                    msg: pb::Pir {
                        count: Some(self.remembered.count),
                        last_ts: Some(now),
                        ..Default::default()
                    },
                    count: self.remembered.count,
                });
            }
        } else {
            self.pending = 0;
        }
        self.last = level;
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn kinds(n: &Vec<News>) -> Vec<&'static str> {
        n.iter().map(|x| match x { News::State(_) => "state", News::Fire { .. } => "fire" }).collect()
    }

    #[test]
    fn a_rising_edge_publishes_high_and_on_ts_as_partial() {
        let mut p = Pir::new(5.0);
        let news = p.tick(true, 100.0);
        assert_eq!(kinds(&news), vec!["state"], "the edge itself is not a scan yet — pending is 1");
        let News::State(m) = &news[0] else { panic!("expected the edge state") };
        assert_eq!(m.high, Some(true));
        assert_eq!(m.on_ts, Some(100.0));
        assert_eq!(m.count, None, "an edge does not invent a count");
        assert_eq!(m.last_hold, None, "a rise does not carry a hold");
    }

    #[test]
    fn a_held_high_fires_on_the_second_consecutive_poll() {
        // The reference's debounce, pinned: pending >= 2 with the level still
        // high. First tick rise (pending=1), second tick high (pending=2) fire.
        let mut p = Pir::new(5.0);
        assert_eq!(kinds(&p.tick(true, 100.0)), vec!["state"]);
        let news = p.tick(true, 100.2);
        assert_eq!(kinds(&news), vec!["fire"]);
        let News::Fire { msg, count } = &news[0] else { panic!("expected fire") };
        assert_eq!(*count, 1);
        assert_eq!(msg.count, Some(1));
        assert_eq!(msg.last_ts, Some(100.2));
        assert_eq!(msg.high, None, "the fire is a record, not a restated level");
    }

    #[test]
    fn a_short_blip_never_counts() {
        // high for ONE poll, then low: pending never reaches 2 — a shadow
        // crossed the room, not a person.
        let mut p = Pir::new(5.0);
        p.tick(true, 100.0);
        let news = p.tick(false, 100.1);
        assert_eq!(kinds(&news), vec!["state"], "only the fall, no fire");
        assert_eq!(p.full().count, Some(0), "the blip is not a motion");
    }

    #[test]
    fn the_cooldown_serialises_fires_but_edges_keep_reporting() {
        let mut p = Pir::new(5.0);
        p.tick(true, 100.0); // rise
        p.tick(true, 100.2); // fire 1
        // person stays: level unchanged, no state news, and no second fire
        // inside the 5s cooldown...
        let news = p.tick(true, 101.0);
        assert!(news.is_empty(), "a held level between polls is neither news");
        // ...until the cooldown passes, then a NEW edge pair is needed to
        // restart pending: fall then rise.
        let news = p.tick(false, 101.5);
        assert_eq!(kinds(&news), vec!["state"], "the fall carries last_hold");
        let News::State(m) = &news[0] else { panic!("expected fall") };
        assert_eq!(m.high, Some(false));
        assert_eq!(m.last_hold, Some(1.5), "101.5 - 100.0 on_ts");
        p.tick(true, 103.0); // new rise, pending = 1
        let news = p.tick(true, 103.2); // pending = 2, 103.2 - 100.2 > 5 -> fire 2
        assert_eq!(kinds(&news), vec!["fire"]);
        let News::Fire { count, .. } = &news[0] else { panic!("expected fire") };
        assert_eq!(*count, 2);
    }

    #[test]
    fn a_fire_inside_the_cooldown_waits_for_the_window_even_across_edges() {
        // The pending counter restarts on a fresh rise, but last_fire does
        // not move: inside the cooldown a double-poll must NOT fire.
        let mut p = Pir::new(5.0);
        p.tick(true, 100.0);
        p.tick(true, 100.2); // fire 1 at 100.2
        p.tick(false, 101.0); // fall
        let news = p.tick(true, 102.0); // rise, pending = 1 -> edge state only
        assert_eq!(kinds(&news), vec!["state"]);
        let news = p.tick(true, 102.2); // pending = 2, but 102.2 - 100.2 < 5
        assert!(news.is_empty(), "the cooldown must hold fire 2 back");
        assert_eq!(p.full().count, Some(1), "still one motion counted, at 100.2");
        // The level kept being tracked through the hold: fall + rise after
        // the window expires fires as normal.
        p.tick(false, 103.0);
        p.tick(true, 106.0);
        let news = p.tick(true, 106.2);
        assert_eq!(kinds(&news), vec!["fire"], "106.2 - 100.2 > 5: the window passed");
        assert_eq!(p.full().count, Some(2));
    }

    #[test]
    fn the_full_payload_is_the_accumulated_union() {
        let mut p = Pir::new(5.0);
        p.seed(false, 0.0);
        p.tick(true, 100.0);
        p.tick(true, 100.2); // fire 1
        p.tick(false, 104.0); // fall, hold 4.0
        let f = p.full();
        assert_eq!(f.high, Some(false));
        assert_eq!(f.count, Some(1));
        assert_eq!(f.last_ts, Some(100.2));
        assert_eq!(f.last_hold, Some(4.0));
        assert_eq!(f.on_ts, None, "the fall dropped on_ts, as the reference's None did");
        assert_eq!(f.ts, None, "ts is the cortex's stamp, never the daemon's");
    }

    #[test]
    fn seeding_a_high_pin_at_boot_reports_high_without_firing() {
        // Daemon starts while someone waves: the boot seed says high with an
        // on_ts, and NO count — the daemon counts what it measured since
        // boot, and it measured no edge. The confirmation still needs two
        // held polls: the seed does not prime the poll cursor.
        let mut p = Pir::new(5.0);
        let m = p.seed(true, 50.0);
        assert_eq!(m.high, Some(true));
        assert_eq!(m.on_ts, Some(50.0));
        assert_eq!(p.full().count, Some(0));
        let news = p.tick(true, 50.4); // first poll: pending = 1, restates the rise
        assert_eq!(kinds(&news), vec!["state"]);
        let news = p.tick(true, 50.8); // pending = 2 -> the very first scan is never throttled
        assert_eq!(kinds(&news), vec!["fire"]);
        assert_eq!(p.full().count, Some(1));
    }
}
