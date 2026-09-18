//! loa-sense — the plumbing every sensor daemon shares, against loa-wire.
//!
//! What it takes to BE a sense daemon: push a reading to the cortex's PULL,
//! hear the cortex's `init` ask on the feed, and answer it with the FULL last
//! payload. Nothing here knows what a PIR is — drivers live with their daemons,
//! same as the Python framework had.
//!
//! NEWS ONLY, NO TICKS. A daemon pushes when it measures something: the sample
//! clock lives here, inside the daemon, and a message rides the wire only when
//! there is a reading behind it. The one exception is the handshake: a fresh
//! daemon publishes its full payload once at start (a cortex already up hears
//! the whole truth without asking), and every daemon re-publishes the full
//! payload each time it hears `init` (a restarted cortex's RAM is empty until
//! it is handed the body back).
//!
//! THE OLD VERSION OF THIS (rust/loa-sense, sense.py-era) held its own PUSH and
//! answered init from a parked thread with a hand-minted SUB. Against the new
//! wire both legs come from loa-wire — `Sender` and `aio::AInbound` — through
//! the ONE libzmq context this struct owns, because inproc endpoints do not
//! cross a context and a socket minted elsewhere pushes into the void.
//!
//! The run loop is `Sense::run_loop`: select over the init ask and the daemon's
//! own sample tick. `--secs N` self-tests fall out of it for free.
pub mod gpio;

use std::collections::HashMap;
use std::future;
use std::sync::atomic::{AtomicU32, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use loa_wire::{aio, envelope, pb};

/// Where readings go: the cortex PULLs here. Loopback, because the senses are
/// ON the body and there is nothing on the far side of the network this side of
/// the topic. Override with LOA_INBOUND.
pub const DEFAULT_INBOUND: &str = "tcp://127.0.0.1:5555";

/// Where the init ask comes FROM (the feed the cortex publishes on). Override
/// with LOA_TOPIC_ENDPOINT — the name the bench scripts have always used, and
/// the one thing in scripts/inject-fault.py that the Python topic.py never
/// actually read. A daemon connecting to 0.0.0.0 must name the host instead;
/// this is the connect endpoint, so it is loopback by default.
pub const DEFAULT_FEED: &str = "tcp://127.0.0.1:5556";

/// Wall-clock seconds since the epoch, as the payloads carry it.
pub fn now_s() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

fn endpoint_from(key: &str, default: &str) -> String {
    match std::env::var(key) {
        Ok(v) if !v.trim().is_empty() => v.trim().to_string(),
        _ => default.to_string(),
    }
}

/// The PUSH endpoint. LOA_INBOUND wins, else the loopback default.
pub fn inbound_endpoint() -> String {
    endpoint_from("LOA_INBOUND", DEFAULT_INBOUND)
}

/// The SUB endpoint for the feed. LOA_TOPIC_ENDPOINT wins, else the default.
pub fn feed_endpoint() -> String {
    endpoint_from("LOA_TOPIC_ENDPOINT", DEFAULT_FEED)
}

/// The hook that answers an init: the daemon's own memory of everything it has
/// measured, as one whole payload.
type FullFn = dyn Fn() -> pb::envelope::Body + Send + Sync;

/// A sense daemon's uplink: the PUSH leg, the init SUB leg, and the full-payload
/// republish hook. One libzmq context for both, per the wire's rule.
pub struct Sense {
    // DECLARATION ORDER IS DROP ORDER, AND IT IS THE BUG THE HISTORY NAMED:
    // libzmq wants sockets closed BEFORE the context terminates — a Context
    // dropped first blocks in zmq_ctx_term forever waiting on sockets that
    // cannot drop because the drop that would free them is parked behind it
    // (hang at exit, reads as a daemon that will not shut down). Sockets
    // first, the context last.
    push: zmq::Socket,
    inbox: aio::AInbound,
    full: Mutex<Option<Arc<FullFn>>>,
    frames: AtomicU32,
    answers: AtomicUsize,
    pub inbound: String,
    pub feed: String,
    // Context cloned into the legs' internals; held here too so the sense
    // owns the context that must outlive every socket in it.
    _ctx: zmq::Context,
}

impl Sense {
    /// Open both legs on ONE libzmq context (inproc endpoints do not cross a
    /// context, and a socket minted elsewhere pushes into the void — the
    /// wire's own lesson). The PUSH leg is built here rather than through
    /// `loa_wire::Sender` for one reason: Sender leaves LINGER at libzmq's
    /// default, INFINITE, and a PUSH holding unsent readings to a down
    /// cortex then BLOCKS FOREVER when the socket drops — the daemon cannot
    /// exit, not even on `--secs`. The history set `linger(1000)` for exactly
    /// this ("a reading is not a frame: it must not be dropped on the way
    /// out", and a daemon that hangs at exit reads as one that will not shut
    /// down). The contract is unchanged: frames still go out through
    /// `loa_wire::encode`, topic-vs-payload checked at the source.
    pub fn open() -> Result<Sense, String> {
        let ctx = zmq::Context::new();
        let inbound = inbound_endpoint();
        let feed = feed_endpoint();
        let push = ctx.socket(zmq::PUSH).map_err(|e| e.to_string())?;
        push.set_linger(1000).map_err(|e| e.to_string())?;
        // Same HIGHWATER as the wire's Sender: a queued pipe to a restarted
        // cortex, bounded — never a stalled daemon.
        push.set_sndhwm(1000).map_err(|e| e.to_string())?;
        push.connect(&inbound).map_err(|e| e.to_string())?;
        let inbox = aio::AInbound::sub(&ctx, &[feed.clone()], &["init"])
            .map_err(|e| e.to_string())?;
        Ok(Sense {
            push,
            inbox,
            full: Mutex::new(None),
            frames: AtomicU32::new(0),
            answers: AtomicUsize::new(0),
            inbound,
            feed,
            _ctx: ctx,
        })
    }

    /// Register the hook that builds the FULL payload. Called once at daemon
    /// start, before the first publish; the payload is derived from the
    /// daemon's own memory, never replayed off the wire.
    pub fn on_full_payload<F>(&self, f: F)
    where
        F: Fn() -> pb::envelope::Body + Send + Sync + 'static,
    {
        *self.full.lock().expect("full hook") = Some(Arc::new(f));
    }

    /// Publish one reading. The topic comes from the payload itself
    /// (`body_name`), and the wire's `encode` refuses a disagreement at the
    /// source — a daemon cannot publish a PIR reading on the ring topic by
    /// mistyping a string, because it never names a topic at all.
    pub fn publish(&self, body: pb::envelope::Body) -> Result<(), String> {
        let env = envelope(body);
        let topic = loa_wire::body_name(&env).ok_or("empty body — nothing to publish")?;
        // The contract's own encoder — topic-vs-payload checked at the source,
        // exactly as `loa_wire::Sender::push` does it; the only difference is
        // this socket's LINGER.
        let [name, payload] = loa_wire::encode(topic, &env).map_err(|e| e.to_string())?;
        self.push
            .send_multipart([name, payload], zmq::DONTWAIT)
            .map_err(|e| e.to_string())?;
        self.frames.fetch_add(1, Ordering::Relaxed);
        Ok(())
    }

    /// A record. Always the `event` topic — an event is not a state.
    pub fn event(&self, kind: &str, detail: &[(&str, &str)]) -> Result<(), String> {
        let ev = pb::Event {
            ts: now_s(),
            kind: kind.to_string(),
            detail: detail
                .iter()
                .map(|(k, v)| (k.to_string(), v.to_string()))
                .collect::<HashMap<_, _>>(),
        };
        self.publish(pb::envelope::Body::Event(ev))
    }

    /// Answer an init ask: republish the FULL payload and nothing else. No
    /// boot event rides the answer — an extra record the cortex never asked
    /// for is the same mistake the old listener called out.
    pub fn republish_full(&self) -> usize {
        let hook = self.full.lock().expect("full hook").clone();
        match hook {
            Some(f) => match self.publish(f()) {
                Ok(()) => self.answers.fetch_add(1, Ordering::Relaxed) + 1,
                Err(_) => 0,
            },
            None => 0,
        }
    }

    /// Wait for the next init ask and return. Drain-first, then await the
    /// socket's readiness: libzmq's ZMQ_FD is level-based, so once the queue
    /// is empty the FD says not-ready and the await parks; the re-arm
    /// re-checks, so no ask is ever lost to the wake-then-drain race. On a
    /// dead socket the daemon goes DEAF (logs once, parks): a run loop whose
    /// init branch returned instantly would republish the full payload on
    /// every select pass — a tick smuggled back in through the side door.
    pub async fn wait_init(&self) {
        loop {
            match self.inbox.recv_nowait() {
                Ok(Some(_)) => return,
                Ok(None) => {}
                Err(e) => {
                    eprintln!("loa-sense: init listener: {e} — going deaf");
                    loop {
                        tokio::time::sleep(Duration::from_secs(3600)).await;
                    }
                }
            }
            if let Err(e) = self.inbox.readable().await {
                eprintln!("loa-sense: init listener: {e} — going deaf");
                loop {
                    tokio::time::sleep(Duration::from_secs(3600)).await;
                }
            }
        }
    }

    /// The daemon's loop: `select!` over the init ask (republish full) and the
    /// sample tick (the daemon's `sample`, called with the sense so it can
    /// publish news). `secs` > 0 ends the loop for the `--secs` self-test.
    ///
    /// `biased` puts the ask first: if a cortex restart and a sample tick are
    /// both ready, the whole truth goes out before the next increment.
    pub async fn run_loop<F>(&self, period: Duration, secs: f64, mut sample: F)
    where
        F: FnMut(&Self),
    {
        let started = Instant::now();
        let mut ticker = tokio::time::interval(period);
        ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        loop {
            if secs > 0.0 && started.elapsed() >= Duration::from_secs_f64(secs) {
                break;
            }
            // The deadline gets its own branch: with a slow tick (the
            // disabled-sonar park runs on an hour) and no init ask — a dev
            // box with no cortex, say — a select over the other two arms
            // could sleep past `--secs` forever. The timer is bounded by
            // what is left, and a self-test that cannot stop is a lie.
            let left = Duration::from_secs_f64(secs.max(0.0)).saturating_sub(started.elapsed());
            let deadline: std::pin::Pin<Box<dyn std::future::Future<Output = ()> + Send>> =
                if secs > 0.0 {
                    Box::pin(tokio::time::sleep(left.max(Duration::from_millis(1))))
                } else {
                    Box::pin(future::pending())
                };
            tokio::select! { biased;
                _ = self.wait_init() => { self.republish_full(); }
                _ = ticker.tick() => { sample(self); }
                _ = deadline => { if secs > 0.0 { break; } }
            }
        }
    }

    /// Frames published (news, full answers, and events all count).
    pub fn frames(&self) -> u32 {
        self.frames.load(Ordering::Relaxed)
    }

    /// Init asks answered.
    pub fn answers(&self) -> usize {
        self.answers.load(Ordering::Relaxed)
    }

    /// The self-test line every sense daemon prints for `--secs`.
    pub fn report(&self, name: &str, secs: f64) {
        println!(
            "{name}: self-test {}s: {} frames published, {} init answers",
            secs,
            self.frames(),
            self.answers()
        );
    }
}

// ---------------------------------------------------------------------------
// Settings: env, then ~/loa.conf JSON, then the built-in default. Carried from
// the sense.py-era daemon verbatim — the same file and truthiness the Python
// config reader parses, because the body's loa.conf was written for it.
// ---------------------------------------------------------------------------

/// A setting, in the order the Python config reader uses: environment first,
/// then loa.conf, then the built-in default.
pub fn setting(env_key: &str, group: &str, key: &str, default: f64) -> f64 {
    if let Ok(v) = std::env::var(env_key) {
        if let Ok(n) = v.trim().parse::<f64>() {
            return n;
        }
    }
    match conf_value(group, key) {
        Some(serde_json::Value::Number(n)) => n.as_f64().unwrap_or(default),
        Some(serde_json::Value::Bool(b)) => {
            if b {
                1.0
            } else {
                0.0
            }
        }
        Some(serde_json::Value::String(s)) => s.parse::<f64>().unwrap_or(default),
        _ => default,
    }
}

/// A string setting — env, then loa.conf, then the default.
pub fn setting_str(env_key: &str, group: &str, key: &str, default: &str) -> String {
    if let Ok(v) = std::env::var(env_key) {
        if !v.is_empty() {
            return v;
        }
    }
    match conf_value(group, key) {
        Some(serde_json::Value::String(s)) => s.clone(),
        Some(serde_json::Value::Bool(b)) => b.to_string(),
        Some(serde_json::Value::Number(n)) => n.to_string(),
        _ => default.to_string(),
    }
}

/// A yes/no setting, with the SAME truthiness the Python reader uses: the
/// offline spellings are the point, because loa.conf carries `snr_enabled:
/// false` for a module that is deliberately unplugged, and a reader that only
/// understood `false` would turn the sonar back on.
///
/// PRESENCE beats emptiness here, unlike `setting_str`: an env var set to ""
/// is an answer ("off" — one of the offline spellings the Python truthiness
/// list carries), while an ABSENT var is no answer at all.
pub fn setting_bool(env_key: &str, group: &str, key: &str, default: bool) -> bool {
    if let Ok(v) = std::env::var(env_key) {
        return truthy(&v);
    }
    match conf_value(group, key) {
        Some(serde_json::Value::Bool(b)) => b,
        Some(serde_json::Value::String(s)) => truthy(&s),
        Some(serde_json::Value::Number(n)) => truthy(&n.to_string()),
        _ => default,
    }
}

fn truthy(raw: &str) -> bool {
    !matches!(
        raw.trim().to_ascii_lowercase().as_str(),
        "0" | "false" | "no" | "off" | ""
    )
}

fn conf_value(group: &str, key: &str) -> Option<serde_json::Value> {
    let path = std::env::var("HOME").unwrap_or_else(|_| "/home/flatline".into()) + "/loa.conf";
    let text = std::fs::read_to_string(&path).ok()?;
    let json: serde_json::Value = serde_json::from_str(&text).ok()?;
    json.get(group)?.get(key).cloned()
}

/// Parse `--secs N` out of the daemon's argv. 0 (absent) means run forever.
pub fn secs_from_args(name: &str) -> f64 {
    let args: Vec<String> = std::env::args().collect();
    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--secs" => {
                let v = args
                    .get(i + 1)
                    .and_then(|s| s.parse::<f64>().ok())
                    .unwrap_or(0.0);
                if v < 0.0 {
                    eprintln!("{name}: --secs must not be negative");
                    std::process::exit(2);
                }
                return v;
            }
            other => {
                eprintln!("{name}: ignoring unknown argument {other}");
            }
        }
        i += 1;
    }
    0.0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn endpoints_default_to_loopback_when_env_is_unset() {
        // The resolution rule, isolated: empty/absent env yields the constant.
        assert_eq!(endpoint_from("LOA_NO_SUCH_VAR_AT_ALL", "tcp://x:1"), "tcp://x:1");
        std::env::set_var("LOA_ENDPOINT_TEST_EMPTY", "   ");
        assert_eq!(endpoint_from("LOA_ENDPOINT_TEST_EMPTY", "tcp://x:1"), "tcp://x:1");
        std::env::set_var("LOA_ENDPOINT_TEST_SET", " tcp://elsewhere:9 ");
        assert_eq!(endpoint_from("LOA_ENDPOINT_TEST_SET", "tcp://x:1"), "tcp://elsewhere:9");
        std::env::remove_var("LOA_ENDPOINT_TEST_EMPTY");
        std::env::remove_var("LOA_ENDPOINT_TEST_SET");
    }

    #[test]
    fn env_beats_loa_conf_for_a_numeric_setting() {
        std::env::set_var("LOA_SENSE_TEST_COOLDOWN", "7.5");
        assert_eq!(
            setting("LOA_SENSE_TEST_COOLDOWN", "sense", "cooldown", 5.0),
            7.5,
            "env did not win the precedence"
        );
        std::env::remove_var("LOA_SENSE_TEST_COOLDOWN");
    }

    #[test]
    fn bool_settings_recognise_every_offline_spelling() {
        for off in ["0", "false", "no", "off", "", "OFF", " No "] {
            std::env::set_var("LOA_SENSE_TEST_FLAG", off);
            assert!(
                !setting_bool("LOA_SENSE_TEST_FLAG", "sense", "snr_enabled", true),
                "{off:?} must mean off — a deliberately unplugged module must stay off"
            );
        }
        std::env::set_var("LOA_SENSE_TEST_FLAG", "yes");
        assert!(setting_bool("LOA_SENSE_TEST_FLAG", "sense", "snr_enabled", false));
        std::env::remove_var("LOA_SENSE_TEST_FLAG");
    }

    #[tokio::test]
    async fn publish_refuses_an_empty_body_rather_than_a_silent_frame() {
        let s = Sense::open().expect("open on loopback, no peer needed");
        assert!(
            s.publish(pb::envelope::Body::Pir(Default::default())).is_ok(),
            "a whole-struct default is still a Pir: absent fields, not an empty oneof"
        );
        // An oneof with nothing set has no body name at all — that is the frame
        // the contract's decode would refuse inbound, so it must not leave here.
        let env = envelope(pb::envelope::Body::Pir(Default::default()));
        let bare = pb::Envelope { schema_version: env.schema_version, body: None };
        assert!(loa_wire::body_name(&bare).is_none(), "bare envelope must have no topic");
        assert_eq!(s.frames(), 1, "the counter counts what went out");
    }

    /// THE HANDSHAKE, OVER REAL SOCKETS: the cortex's init ask on the feed
    /// reaches the SUB leg through the skeleton's wait, and the answer off
    /// the PUSH leg is the daemon's FULL payload — every field the hook
    /// builds, not a change. This is the one behaviour the skeleton exists
    /// for; everything else is counting.
    #[test]
    fn an_init_ask_on_the_feed_earns_a_full_payload_on_the_inbound() {
        // Isolated ports so this cannot trade frames with anything else. A
        // plain #[test]: the ask loop and the PULL drain block, and only
        // wait_init awaits, on a current-thread runtime built in here.
        std::env::set_var("LOA_TOPIC_ENDPOINT", "tcp://127.0.0.1:15556");
        std::env::set_var("LOA_INBOUND", "tcp://127.0.0.1:15555");
        let r = std::thread::spawn(|| {
            let rt = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .unwrap();
            let ctx = zmq::Context::new();
            let pub_sock =
                loa_wire::Publisher::bind(&ctx, "tcp://127.0.0.1:15556").expect("bind feed");
            let pull =
                loa_wire::Receiver::bind(&ctx, "tcp://127.0.0.1:15555").expect("bind inbound");
            // The Sense is opened INSIDE the runtime: AInbound registers
            // zmq's FD with the reactor at construction (AsyncFd::new), and
            // that needs a runtime current — the daemons' #[tokio::main]
            // always gives them one; a test must too.
            let sense = rt
                .block_on(async { Sense::open().expect("open on the isolated ports") });
            sense.on_full_payload(|| {
                pb::envelope::Body::Pir(pb::Pir {
                    high: Some(true),
                    count: Some(7),
                    last_ts: Some(111.0),
                    last_hold: Some(2.2),
                    on_ts: Some(109.0),
                    ts: None,
                })
            });
            // The ask, repeated: PUB/SUB is a slow joiner — the cortex
            // publishes init six times for exactly this reason; the answer
            // must arrive once, no matter which ask the SUB first caught.
            let asker = std::thread::spawn(move || {
                for _ in 0..40 {
                    let ask = envelope(pb::envelope::Body::Init(pb::Init {
                        source: Some("test".into()),
                        ts: Some(1.0),
                    }));
                    let _ = pub_sock.publish("init", &ask);
                    std::thread::sleep(Duration::from_millis(50));
                }
            });
            let found = rt.block_on(async {
                sense.wait_init().await;
                sense.republish_full();
                let deadline = Instant::now() + Duration::from_secs(5);
                loop {
                    if let Some(parts) = pull.recv_nowait().expect("pull") {
                        let m = loa_wire::decode(parts).expect("contract-valid answer");
                        if let Some(pb::envelope::Body::Pir(p)) = m.env.body {
                            if p.count == Some(7) {
                                assert_eq!(m.topic, "pir", "the answer rode its own topic");
                                assert_eq!(p.high, Some(true), "the FULL payload, not a change");
                                assert_eq!(p.last_ts, Some(111.0));
                                assert_eq!(p.on_ts, Some(109.0));
                                assert_eq!(p.last_hold, Some(2.2));
                                break true;
                            }
                        }
                    }
                    assert!(Instant::now() < deadline, "no full-payload answer within 5s");
                    tokio::time::sleep(Duration::from_millis(10)).await;
                }
            });
            assert_eq!(sense.answers(), 1, "wait_init returned once and answered once");
            assert!(found, "the handshake completed");
            drop(sense);
            let _ = asker.join();
        });
        r.join().expect("handshake test thread");
        std::env::remove_var("LOA_TOPIC_ENDPOINT");
        std::env::remove_var("LOA_INBOUND");
    }

    #[test]
    fn run_loop_ticks_the_sample_and_stops_at_secs() {
        // --secs N semantics: the loop runs N seconds of sample ticks and
        // RETURNS; it does not hang and it does not spin without waiting.
        let hits = Arc::new(std::sync::atomic::AtomicU32::new(0));
        let h = Arc::clone(&hits);
        let sense = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap()
            .block_on(async move {
                let s = Sense::open().expect("open");
                s.run_loop(Duration::from_millis(50), 0.6, move |_| {
                    h.fetch_add(1, Ordering::Relaxed);
                })
                .await;
                s
            });
        sense.report("loa-test", 0.6);
        let n = hits.load(Ordering::Relaxed);
        assert!((3..=20).contains(&n), "0.6s of 50ms ticks must be ~12, got {n}");
    }
}
