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

use std::time::{Duration, Instant};

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

/// One decoded message off the wire.
pub struct Envelope {
    pub topic: String,
    pub env: pb::Envelope,
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
}

pub struct Subscriber {
    sock: zeromq::SubSocket,
    pub endpoints: Vec<String>,
}

impl Subscriber {
    pub async fn connect(endpoints: &[String], topics: &[&str]) -> Result<Self> {
        use zeromq::Socket;
        let mut sock = zeromq::SubSocket::new();
        for t in topics {
            sock.subscribe(t).await.map_err(|e| Error::Zmq(e.to_string()))?;
        }
        for ep in endpoints {
            sock.connect(ep.as_str())
                .await
                .map_err(|e| Error::Zmq(e.to_string()))?;
        }
        Ok(Self {
            sock,
            endpoints: endpoints.to_vec(),
        })
    }

    /// The next message, or None if the timeout expires first.
    ///
    /// The timeout exists so a consumer can do something else between frames
    /// (draw, blit) without a second thread: a subscriber parked forever in
    /// recv() cannot notice that a whole second has gone by with no feed.
    pub async fn recv_timeout(&mut self, ms: u64) -> Result<Option<Envelope>> {
        use zeromq::SocketRecv;
        match tokio::time::timeout(Duration::from_millis(ms), self.sock.recv()).await {
            Err(_) => Ok(None),
            Ok(Ok(msg)) => Ok(Some(decode(msg)?)),
            Ok(Err(e)) => Err(Error::Zmq(e.to_string())),
        }
    }

    /// Everything already waiting, oldest first, without blocking.
    ///
    /// This is how a frame consumer stays on the NEWEST frame instead of
    /// replaying a backlog: drain what has piled up, keep the last. The Python
    /// side needed a socket option for this; here it is a loop, and the loop is
    /// also where the drop count comes from.
    pub async fn drain(&mut self) -> Result<Vec<Envelope>> {
        let mut out = Vec::new();
        loop {
            let got = self.recv_timeout(if out.is_empty() { 1 } else { 0 }).await?;
            match got {
                Some(env) => out.push(env),
                None => return Ok(out),
            }
        }
    }
}

fn decode(msg: zeromq::ZmqMessage) -> Result<Envelope> {
    if msg.len() != 2 {
        return Err(Error::Framing(msg.len()));
    }
    let topic = String::from_utf8_lossy(msg.get(0).expect("len checked")).to_string();
    let raw = msg.get(1).expect("len checked");
    let env = pb::Envelope::decode(raw.as_ref())
        .map_err(|e| Error::Zmq(format!("envelope did not decode: {e}")))?;
    if env.schema_version != SCHEMA_VERSION {
        return Err(Error::SchemaMismatch(env.schema_version));
    }
    let body = body_name(&env);
    if body != topic {
        return Err(Error::TopicMismatch { topic, body });
    }
    Ok(Envelope { topic, env })
}

/// The oneof's variant name, as a string — the same name the topic frame must
/// carry. Hand-written on purpose: prost generates an enum, and matching it
/// exhaustively here is what makes a NEW topic a compile error in this file
/// rather than a message nobody can decode.
pub fn body_name(env: &pb::Envelope) -> String {
    use pb::envelope::Body;
    match env.body {
        None => "<empty>".into(),
        Some(Body::Ripperdoc(_)) => "ripperdoc".into(),
        Some(Body::Ring(_)) => "ring".into(),
        Some(Body::Pir(_)) => "pir".into(),
        Some(Body::Sonar(_)) => "sonar".into(),
        Some(Body::Baro(_)) => "baro".into(),
        Some(Body::Weather(_)) => "weather".into(),
        Some(Body::Power(_)) => "power".into(),
        Some(Body::Fault(_)) => "fault".into(),
        Some(Body::Event(_)) => "event".into(),
        Some(Body::Init(_)) => "init".into(),
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
