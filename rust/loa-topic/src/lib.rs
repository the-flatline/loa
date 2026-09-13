//! loa-topic — the wire, in Rust.
//!
//! The same contract `loa/topic.py` speaks: every message is TWO ZMQ FRAMES,
//! `[topic][Envelope]`, with the topic name as the subscription filter and the
//! Envelope's oneof as the type check. Both halves are checked here too — a
//! message whose topic and payload disagree is refused, never guessed at.
//!
//! There is no fallback to HTTP and no side channel: the topic is the only way
//! data reaches a consumer, in either language. The Python console regressed
//! into a `/state` poll once already and it took an AST test to stop it coming
//! back; the Rust side simply has nothing else to call.
//!
//! The protobuf types are GENERATED (see build.rs) from `proto/loa.proto`, so
//! the schema has one definition. `SCHEMA_VERSION` is the number that must match
//! the publisher's, and it is duplicated here on purpose: a consumer that reads
//! the version from the message it is about to distrust has nothing to compare
//! against.

use std::time::Instant;

pub mod pb {
    include!(concat!(env!("OUT_DIR"), "/loa.rs"));
}

pub use prost::Message as ProstMessage;

/// Must equal `loa/topic.py`'s. A mismatch is refused loudly: a consumer that
/// guesses at a newer schema draws a plausible, wrong picture.
pub const SCHEMA_VERSION: u32 = 8;

/// The feed's port on the body. The cortex BINDS tcp://0.0.0.0:5556 — an
/// address a client cannot connect to — so consumers connect to the body's host
/// on this port instead.
pub const FEED_PORT: u16 = 5556;

/// The topics the cortex publishes. Kept identical to `loa/topic.py`'s TOPICS:
/// a consumer that subscribes to a name nobody publishes waits forever and
/// looks exactly like a body that is not talking.
pub const TOPICS: [&str; 10] = [
    "ripperdoc", "ring", "pir", "sonar", "baro", "weather", "power", "fault",
    "event", "init",
];

#[derive(Debug)]
pub enum Error {
    /// Publisher and subscriber disagree about the wire format.
    SchemaMismatch(u32),
    /// The topic frame and the payload inside it disagree.
    TopicMismatch { topic: String, body: String },
    /// A ZMQ message that is not two frames.
    Framing(usize),
    Zmq(String),
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Error::SchemaMismatch(v) => write!(
                f,
                "wire schema v{v}, this build speaks v{SCHEMA_VERSION} — refusing to guess at the fields"
            ),
            Error::TopicMismatch { topic, body } => write!(
                f,
                "topic frame says {topic:?}, payload is {body:?} — refusing to guess which half is right"
            ),
            Error::Framing(n) => {
                write!(f, "a message arrived with {n} frames, not 2 ([topic][envelope])")
            }
            Error::Zmq(e) => write!(f, "zmq: {e}"),
        }
    }
}

impl std::error::Error for Error {}

pub type Result<T> = std::result::Result<T, Error>;

/// Where to subscribe. `LOA_TOPIC_ENDPOINT` names it outright; otherwise it is
/// the same host the HTTP door lives behind, on the feed's port — the console
/// runs on dixie, so with `LOA_API_BIND=192.168.1.200` the feed is
/// tcp://192.168.1.200:5556.
pub fn feed_endpoints() -> Vec<String> {
    if let Ok(ep) = std::env::var("LOA_TOPIC_ENDPOINT") {
        if !ep.is_empty() {
            return vec![ep];
        }
    }
    let host = std::env::var("LOA_API_BIND").unwrap_or_else(|_| "127.0.0.1".into());
    vec![format!("tcp://{host}:{FEED_PORT}")]
}

/// One decoded message off the wire. `frames` is how many ZMTP frames it
/// arrived in: 2 is the contract, more means the peer chunked the body and this
/// is the number it actually took (see `decode`).
pub struct Envelope {
    pub topic: String,
    pub env: pb::Envelope,
    pub frames: usize,
}

impl Envelope {
    /// The ripperdoc payload, when this message is one. The face rides inside
    /// it, and only inside it.
    pub fn ripperdoc(&self) -> Option<&pb::Ripperdoc> {
        match self.env.body.as_ref() {
            Some(pb::envelope::Body::Ripperdoc(m)) => Some(m),
            _ => None,
        }
    }

    /// The ring payload: 72 bytes of RGB, its own topic because wanting the
    /// ring is its own interest.
    pub fn ring(&self) -> Option<&pb::Ring> {
        match self.env.body.as_ref() {
            Some(pb::envelope::Body::Ring(m)) => Some(m),
            _ => None,
        }
    }
}

pub struct Subscriber {
    /// The context must outlive the socket, so it is held here and not made
    /// per call: dropping a libzmq context closes its sockets.
    _ctx: zmq::Context,
    sock: zmq::Socket,
    pub endpoints: Vec<String>,
}

impl Subscriber {
    pub fn connect(endpoints: &[String], topics: &[&str]) -> Result<Self> {
        let ctx = zmq::Context::new();
        let sock = ctx.socket(zmq::SUB).map_err(zmq_err)?;
        sock.set_linger(0).map_err(zmq_err)?;
        // NEVER REPLAY A BACKLOG — but NOT with ZMQ_CONFLATE. Measured
        // 2026-09-14: with conflate set on this SUB, libzmq accepted the option
        // and then delivered ZERO messages (0 in 3s at 120/s on the wire, every
        // topic empty); with it off, 611 messages in the same 3s. It is
        // documented for SUB and it fails silently, so it is not used here —
        // an option that costs the entire feed is worse than the backlog it was
        // meant to prevent.
        //
        // RCVHWM=1 instead: the inbound queue holds one message per pipe, so a
        // consumer that falls behind has at most one frame waiting and the rest
        // are dropped at the socket. `drain()` then keeps the LAST of what
        // arrives, which is the behaviour we actually wanted: a consumer shows
        // the newest frame it can and never works through old ones.
        sock.set_rcvhwm(1).map_err(zmq_err)?;
        if std::env::var("LOA_CONFLATE").is_ok() {
            sock.set_conflate(true).map_err(zmq_err)?;
        }
        for t in topics {
            sock.set_subscribe(t.as_bytes()).map_err(zmq_err)?;
        }
        for ep in endpoints {
            sock.connect(ep).map_err(zmq_err)?;
        }
        Ok(Self {
            _ctx: ctx,
            sock,
            endpoints: endpoints.to_vec(),
        })
    }

    /// The next message, or None if the timeout expires first.
    ///
    /// The timeout exists so a consumer can do something else between frames
    /// (blit, draw) without a second thread: a subscriber parked forever in
    /// recv() cannot notice that a whole second has gone by with no feed.
    pub fn recv_timeout(&mut self, ms: i64) -> Result<Option<Envelope>> {
        match self.sock.poll(zmq::POLLIN, ms) {
            Ok(0) => Ok(None),
            Ok(_) => {
                let parts = self.sock.recv_multipart(0).map_err(zmq_err)?;
                Ok(Some(decode(parts)?))
            }
            Err(e) => Err(zmq_err(e)),
        }
    }

    /// Everything already waiting, oldest first, without blocking.
    ///
    /// With CONFLATE set there is at most one message waiting, so this is a
    /// shape consumers can still use without knowing which socket option is on.
    pub fn drain(&mut self) -> Result<Vec<Envelope>> {
        let mut out = Vec::new();
        loop {
            let got = self.recv_timeout(if out.is_empty() { 1 } else { 0 })?;
            match got {
                Some(env) => out.push(env),
                None => return Ok(out),
            }
        }
    }
}

fn zmq_err(e: zmq::Error) -> Error {
    Error::Zmq(e.to_string())
}

fn decode(parts: Vec<Vec<u8>>) -> Result<Envelope> {
    // STRICTLY two frames, and now that is a safe thing to demand: both ends
    // are libzmq, which reassembles ZMTP chunks before the API sees a message.
    // The earlier version of this file joined frames 1..n to survive a
    // three-frame arrival — a symptom of a second implementation of the framing
    // rules on the other side, not of a fault on the wire. Joining hid it;
    // counting it names it.
    if parts.len() != 2 {
        return Err(Error::Framing(parts.len()));
    }
    let topic = String::from_utf8_lossy(&parts[0]).to_string();
    let env = pb::Envelope::decode(parts[1].as_slice())
        .map_err(|e| Error::Zmq(format!("envelope did not decode: {e}")))?;
    if env.schema_version != SCHEMA_VERSION {
        return Err(Error::SchemaMismatch(env.schema_version));
    }
    let body = body_name(&env);
    if body != topic {
        return Err(Error::TopicMismatch { topic, body });
    }
    Ok(Envelope {
        topic,
        env,
        frames: parts.len(),
    })
}

/// The oneof's variant name, as a string — the same name the topic frame must
/// carry. Hand-written on purpose: prost generates an enum, and matching it
/// exhaustively here is what makes a NEW topic a compile error in this file
/// rather than a message nobody can decode.
pub fn body_name(env: &pb::Envelope) -> String {
    match env.body.as_ref() {
        Some(b) => body_topic_of(b).to_string(),
        None => "<empty>".into(),
    }
}


/// The topic a payload MUST travel under.
///
/// At the sending end this removes a whole class of fault: there is no way to
/// publish a pir reading on the ring topic, because the sender asks the payload
/// what it is. The receiving end checks the same thing independently (ZMQ
/// matches the subscription against the first frame, protobuf types the body),
/// which is the belt-and-braces the wire contract asks for.
pub fn body_topic_of(body: &pb::envelope::Body) -> &'static str {
    use pb::envelope::Body;
    match body {
        Body::Ripperdoc(_) => "ripperdoc",
        Body::Ring(_) => "ring",
        Body::Pir(_) => "pir",
        Body::Sonar(_) => "sonar",
        Body::Baro(_) => "baro",
        Body::Weather(_) => "weather",
        Body::Power(_) => "power",
        Body::Fault(_) => "fault",
        Body::Event(_) => "event",
        Body::Init(_) => "init",
    }
}

/// A rolling rate, because "how fast is it really" is a number we report and
/// not a feeling we have about it.
pub struct Rate {
    start: Instant,
    n: u64,
}

impl Rate {
    pub fn new() -> Self {
        Self {
            start: Instant::now(),
            n: 0,
        }
    }

    pub fn tick(&mut self) {
        self.n += 1;
    }

    pub fn fps(&self) -> f64 {
        let el = self.start.elapsed().as_secs_f64();
        if el <= 0.0 {
            0.0
        } else {
            self.n as f64 / el
        }
    }

    pub fn elapsed(&self) -> f64 {
        self.start.elapsed().as_secs_f64()
    }

    pub fn reset(&mut self) -> (u64, f64) {
        let el = self.start.elapsed().as_secs_f64();
        let n = self.n;
        self.start = Instant::now();
        self.n = 0;
        (n, el)
    }
}

impl Default for Rate {
    fn default() -> Self {
        Self::new()
    }
}
